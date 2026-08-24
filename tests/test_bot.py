"""The bot object itself: wiring, event routing and shutdown.

`hell/bot.py` is the one module a unit test cannot reach through the engine, so
it gets its own — no gateway connection is made anywhere here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

import discord
import pytest

from hell.bot import HellBot, build_bot
from hell.config import ConfigError
from tests.test_monitor import FakeMember


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(config):
    instance = build_bot(config)
    yield instance
    instance.store.close()


# ------------------------------------------------------------------- wiring


def test_every_subsystem_is_wired_together(bot, config):
    assert isinstance(bot, HellBot)
    assert bot.engine.store is bot.store
    assert bot.announcer.engine is bot.engine
    assert bot.monitor.engine is bot.engine
    assert bot.monitor.announcer is bot.announcer
    assert bot.monitor.reports.engine is bot.engine
    assert bot.log_stream.config is config


def test_required_intents_are_requested(bot):
    assert bot.intents.members       # needed to see who is in the VC
    assert bot.intents.voice_states
    assert bot.intents.guilds
    # Without message content, alive-check replies arrive empty and nobody can
    # ever answer a roll call — this must never regress.
    assert bot.intents.message_content


def test_the_log_stream_captures_records_before_login(bot):
    import logging

    logging.getLogger("hell.test").warning("early warning")
    lines, _dropped = bot.log_stream.handler.drain()
    assert any("early warning" in line for line in lines)


# ------------------------------------------------------------ event routing


def test_messages_are_offered_to_the_alive_check(bot, monkeypatch):
    seen: list[str] = []

    async def handler(message):
        seen.append(message.content)

    monkeypatch.setattr(bot.monitor, "handle_message", handler)
    monkeypatch.setattr(bot, "process_commands", _noop)

    message = MagicMock(spec=discord.Message)
    message.guild = MagicMock()
    message.author.bot = False
    message.content = "Yes"

    run(bot.on_message(message))
    assert seen == ["Yes"]


def test_dms_and_bot_messages_are_ignored(bot, monkeypatch):
    seen: list[str] = []

    async def handler(message):
        seen.append(message.content)

    monkeypatch.setattr(bot.monitor, "handle_message", handler)
    monkeypatch.setattr(bot, "process_commands", _noop)

    dm = MagicMock(spec=discord.Message)
    dm.guild = None
    dm.author.bot = False
    dm.content = "Yes"
    run(bot.on_message(dm))

    from_bot = MagicMock(spec=discord.Message)
    from_bot.guild = MagicMock()
    from_bot.author.bot = True
    from_bot.content = "Yes"
    run(bot.on_message(from_bot))

    assert seen == []


def test_a_failing_message_handler_never_escapes(bot, monkeypatch, caplog):
    import logging

    async def boom(_message):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(bot.monitor, "handle_message", boom)
    monkeypatch.setattr(bot, "process_commands", _noop)

    message = MagicMock(spec=discord.Message)
    message.guild = MagicMock()
    message.author.bot = False
    message.content = "Yes"

    with caplog.at_level(logging.ERROR, logger="hell"):
        run(bot.on_message(message))          # must not raise
    assert "handler exploded" in caplog.text


def test_clankers_are_kicked_the_moment_they_join(bot, config):
    member = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = config.voice_channel_id

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == [None]


def test_joining_another_channel_is_ignored(bot, config):
    member = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = 999999                  # a different VC

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == []


def test_regular_members_are_left_alone(bot, config):
    member = FakeMember(3, "Alice")
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = config.voice_channel_id

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == []


# ---------------------------------------------------------------- shutdown


def test_close_stops_the_loops_and_the_database(config):
    instance = build_bot(config)

    async def scenario():
        await instance.close()

    run(scenario())

    assert not instance.monitor._monitor_loop.is_running()
    with pytest.raises(sqlite3.ProgrammingError):
        instance.store.load_state()            # the connection really is closed


# ------------------------------------------------------------ configuration


def test_main_exits_cleanly_without_configuration(monkeypatch, capsys):
    from hell import bot as bot_module

    def missing():
        raise ConfigError("Missing required environment variable: DISCORD_TOKEN")

    monkeypatch.setattr(bot_module.Config, "from_env", staticmethod(missing))

    with pytest.raises(SystemExit) as exit_info:
        bot_module.main()

    assert exit_info.value.code == 2
    assert "DISCORD_TOKEN" in capsys.readouterr().err


async def _noop(*_args, **_kwargs):
    return None


# ------------------------------------------------------------- startup path


def test_setup_hook_registers_the_commands_and_syncs(bot, monkeypatch):
    synced: dict = {}

    async def fake_sync(*, guild=None):
        synced["guild"] = getattr(guild, "id", None)
        return [1, 2, 3]

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    run(bot.setup_hook())

    assert synced["guild"] == bot.config.guild_id
    assert bot.get_cog("hell") is not None


def test_setup_hook_survives_a_failed_sync(bot, monkeypatch, caplog):
    import logging

    async def boom(*, guild=None):
        raise discord.HTTPException(MagicMock(status=500), "discord is down")

    monkeypatch.setattr(bot.tree, "sync", boom)
    with caplog.at_level(logging.ERROR, logger="hell"):
        run(bot.setup_hook())        # must not stop the bot from running
    assert "Could not sync slash commands" in caplog.text


def test_on_ready_reports_state_runs_preflight_and_starts_the_monitor(bot, monkeypatch, caplog):
    import logging

    from hell.health import HealthReport

    calls: list[str] = []

    async def fake_preflight(_bot, _config):
        calls.append("preflight")
        report = HealthReport()
        report.warnings.append("something to look at")
        return report

    async def fake_presence(*_a, **_kw):
        calls.append("presence")

    async def fake_resume():
        calls.append("resume")

    bot.config.token = "super-secret-token-value"     # so the check below means something
    monkeypatch.setattr("hell.bot.preflight", fake_preflight)
    monkeypatch.setattr(bot, "change_presence", fake_presence)
    monkeypatch.setattr(bot.monitor, "resume_after_restart", fake_resume)
    monkeypatch.setattr(bot.monitor, "start", lambda: calls.append("monitor"))
    monkeypatch.setattr(bot.log_stream, "start", _noop)
    monkeypatch.setattr(bot, "_start_status_rotation", lambda: None)
    monkeypatch.setattr("hell.bot.start_server", _noop)

    with caplog.at_level(logging.INFO, logger="hell"):
        run(bot.on_ready())

    assert calls == ["preflight", "presence", "monitor", "resume"]
    assert bot.health is not None and bot.health.warnings
    assert "Voice channel:" in caplog.text          # the redacted config summary
    assert bot.config.token not in caplog.text


def test_on_ready_recovers_only_once(bot, monkeypatch):
    resumes: list[int] = []

    async def fake_preflight(_bot, _config):
        from hell.health import HealthReport

        return HealthReport()

    monkeypatch.setattr("hell.bot.preflight", fake_preflight)
    monkeypatch.setattr(bot, "change_presence", _noop)
    monkeypatch.setattr(bot.monitor, "start", lambda: None)
    monkeypatch.setattr(bot.log_stream, "start", _noop)
    monkeypatch.setattr(bot.monitor, "resume_after_restart",
                        lambda: _record(resumes))
    monkeypatch.setattr(bot, "_start_status_rotation", lambda: None)
    monkeypatch.setattr("hell.bot.start_server", _noop)

    run(bot.on_ready())
    run(bot.on_ready())          # a reconnect fires on_ready again

    assert len(resumes) == 1     # recovery must not run twice


def test_on_ready_survives_a_broken_preflight(bot, monkeypatch, caplog):
    import logging

    async def boom(_bot, _config):
        raise RuntimeError("preflight exploded")

    monkeypatch.setattr("hell.bot.preflight", boom)
    monkeypatch.setattr(bot, "change_presence", _noop)
    monkeypatch.setattr(bot.monitor, "start", lambda: None)
    monkeypatch.setattr(bot.monitor, "resume_after_restart", _noop)
    monkeypatch.setattr(bot.log_stream, "start", _noop)
    monkeypatch.setattr(bot, "_start_status_rotation", lambda: None)
    monkeypatch.setattr("hell.bot.start_server", _noop)

    with caplog.at_level(logging.ERROR, logger="hell"):
        run(bot.on_ready())      # the bot must still come up
    assert "Preflight checks failed to run" in caplog.text


def test_presence_reflects_the_event_state(bot, monkeypatch):
    from tests.conftest import T0, start

    shown: list[str] = []

    async def capture(activity=None):
        shown.append(activity.name if activity else "")

    monkeypatch.setattr(bot, "change_presence", capture)

    run(bot._update_presence())
    assert "/hell start" in shown[-1]

    start(bot.engine, T0, 1)
    run(bot._update_presence())
    assert "Hell:" in shown[-1] and "160h" in shown[-1]


async def _record(bucket):
    bucket.append(1)
