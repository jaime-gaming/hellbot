"""Smoke tests for the Discord wiring (no gateway connection is made)."""

from __future__ import annotations

import discord
import pytest
from discord.ext import commands

from hell.announcer import Announcer
from hell.cog import HellCommands
from hell.monitor import VoiceMonitor
from hell.ui import CODE_LIFETIME_SECONDS, CodeGate


@pytest.fixture
def bot(config):
    intents = discord.Intents.default()
    intents.members = True
    intents.voice_states = True
    b = commands.Bot(command_prefix="!", intents=intents)
    b.config = config  # type: ignore[attr-defined]
    return b


def test_command_group_exposes_the_expected_subcommands(bot, config, engine):
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    names = {cmd.name for cmd in cog.app_command.commands}  # type: ignore[union-attr]
    assert names == {
        "start", "status", "leaderboard", "milestones", "mystats", "user", "help",
        "alivecheck", "logs", "reloadmessages", "doctor", "export", "stop", "reset",
        "approve", "pause", "resume", "restart", "security", "errors",
    }
    assert cog.app_command.name == "hell"  # type: ignore[union-attr]


def test_restricted_commands_carry_a_check(bot, config, engine):
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    by_name = {c.name: c for c in cog.app_command.commands}  # type: ignore[union-attr]
    for restricted in (
        "start", "stop", "reset", "approve", "pause", "resume", "restart", "security",
        "alivecheck", "logs", "reloadmessages", "doctor", "export",
    ):
        assert by_name[restricted].checks, f"/hell {restricted} must be host-restricted"
    for public in ("status", "leaderboard", "milestones", "mystats", "user", "help"):
        assert not by_name[public].checks


# ------------------------------------------------------------- approval codes


def test_approval_codes_are_short_lived_and_single_use():
    gate = CodeGate()
    code = gate.issue("stop")
    assert len(code) == 6
    assert code.isalnum()
    assert gate.pending_action == "stop"

    # Case-insensitive, then gone forever.
    assert gate.redeem("stop", code.lower()) is None
    assert gate.redeem("stop", code) is not None          # already consumed
    assert gate.pending_action is None


def test_approval_codes_are_tied_to_their_action():
    gate = CodeGate()
    code = gate.issue("stop")
    assert "different" in gate.redeem("reset", code)
    assert gate.redeem("stop", "ZZZZZZ") == "that code is not correct"


def test_approval_codes_expire():
    import time

    gate = CodeGate()
    gate.issue("stop")
    gate._pending.issued_at = time.time() - (CODE_LIFETIME_SECONDS + 1)
    assert gate.pending_action is None                    # gone once stale
    assert gate.expired_action == "stop"                  # …and reported


def test_approval_codes_can_be_invalidated():
    gate = CodeGate()
    gate.issue("reset")
    gate.invalidate()
    assert gate.pending_action is None
    assert gate.expired_action is None


def test_approval_codes_bind_to_their_event():
    """A code issued for event A must never be redeemable against event B."""
    gate = CodeGate()
    gate.issue("stop", event_uid="evt-A")
    assert gate.pending_event_uid == "evt-A"
    assert gate.redeem("stop", "X" * 6) == "that code is not correct"   # still pending
    gate.issue("reset", event_uid="evt-B")                              # reissue rebinds
    assert gate.pending_event_uid == "evt-B"


def test_monitor_intervals_follow_config(bot, config, engine):
    monitor = VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))
    assert monitor._monitor_loop.seconds == 1.0
    assert monitor._progress_loop.seconds == 20.0


def test_background_tasks_report_their_failures(caplog):
    """A fire-and-forget task must never fail silently (hell/tasks.py)."""
    import asyncio
    import logging

    from hell import tasks

    async def boom():
        raise RuntimeError("task went wrong")

    async def scenario():
        task = tasks.spawn(boom(), name="unit-test")
        assert tasks.active() == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return task

    with caplog.at_level(logging.ERROR, logger="hell.tasks"):
        asyncio.run(scenario())

    assert "unit-test" in caplog.text and "task went wrong" in caplog.text
    assert tasks.active() == 0


def test_spawned_tasks_are_kept_alive_until_they_finish():
    """Without a strong reference the event loop may collect a running task."""
    import asyncio

    from hell import tasks

    async def scenario():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.01)
            return "done"

        task = tasks.spawn(work(), name="keepalive")
        await started.wait()
        assert tasks.active() == 1
        assert await task == "done"

    asyncio.run(scenario())
    assert tasks.active() == 0


def test_config_summary_never_leaks_the_token(config):
    values = [value for _label, value in config.summary()]
    assert config.token not in values
    assert any("Empty-VC grace" == label for label, _ in config.summary())
