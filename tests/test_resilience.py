"""Error and recovery branches: what happens when Discord says no.

Most remaining uncovered lines were `except discord.Forbidden` style paths.
They are the ones that decide whether a bad permission is a logged warning or
a dead event, so they get tested like everything else.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import discord
import pytest

from hell import paths, tasks
from hell.announcer import Announcer
from hell.engine import MilestoneReached
from hell.milestones import get_milestone
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from tests.conftest import GRACE, T0, obs, start
from tests.test_integration import FakeTextChannel
from tests.test_monitor import FakeBot as MonitorBot
from tests.test_monitor import FakeMember, FakeVoiceChannel


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status=403):
        self.status = status
        self.reason = "err"


class HostileChannel(FakeTextChannel):
    """A channel that refuses everything."""

    def __init__(self, error, cid=2):
        super().__init__(cid)
        self.error = error

    async def send(self, content=None, **kwargs):
        raise self.error


class BotWith:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _cid):
        return self.channel

    async def fetch_channel(self, cid):
        raise discord.NotFound(_Resp(404), f"no channel {cid}")


# ------------------------------------------------------------- announcing


def test_a_forbidden_channel_is_explained_not_crashed(config, engine, caplog):
    announcer = Announcer(BotWith(HostileChannel(discord.Forbidden(_Resp(), "nope"))), config, engine)
    start(engine, T0, 1)

    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        result = run(announcer.send([announcer.build_progress(engine.snapshot(now=T0))],
                                    content="@everyone", mention_everyone=True))

    assert result is None
    assert "Missing permission" in caplog.text
    assert "Mention @everyone" in caplog.text          # names the exact permission


def test_a_rate_limited_send_is_logged(config, engine, caplog):
    error = discord.HTTPException(_Resp(429), "slow down")
    announcer = Announcer(BotWith(HostileChannel(error)), config, engine)
    start(engine, T0, 1)

    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        assert run(announcer.send([discord.Embed(title="x")])) is None
    assert "Failed to send announcement" in caplog.text


def test_a_milestone_is_not_marked_announced_when_the_send_fails(config, engine):
    """It must be retried on the next start, not silently lost."""
    announcer = Announcer(BotWith(HostileChannel(discord.Forbidden(_Resp(), "nope"))), config, engine)
    start(engine, T0, 1)
    engine.store.claim_milestone(engine.event_uid, 32, T0)

    run(announcer.announce_milestone(
        MilestoneReached(milestone=get_milestone(32), reached_ts=T0, members=[])
    ))

    assert [m.hours for m in engine.pending_announcements()] == [32]


def test_a_missing_channel_is_reported_once(config, engine, caplog):
    announcer = Announcer(BotWith(None), config, engine)
    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        assert run(announcer.channel()) is None
    assert "unreachable" in caplog.text


def test_a_non_text_channel_is_rejected(config, engine, caplog):
    announcer = Announcer(BotWith(object()), config, engine)
    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        assert run(announcer.channel()) is None
    assert "not a text channel" in caplog.text


# ------------------------------------------------- the live progress message


class FlakyMessage:
    def __init__(self, error=None):
        self.id = 5
        self.channel = MagicMock(id=2)
        self.error = error
        self.edits = 0

    async def edit(self, **_kwargs):
        if self.error:
            raise self.error
        self.edits += 1

    async def pin(self, reason=None):
        raise discord.HTTPException(_Resp(400), "cannot pin")


def test_a_deleted_progress_message_is_recreated(config, engine):
    channel = FakeTextChannel()
    announcer = Announcer(BotWith(channel), config, engine)
    start(engine, T0, 1)
    announcer._progress_message = FlakyMessage(discord.NotFound(_Resp(404), "gone"))

    run(announcer.update_progress(engine.snapshot(now=T0 + 60), force=True))

    assert len(channel.sent) == 1                      # posted a fresh one
    assert engine.state.progress_message_id is not None


def test_a_forbidden_edit_gives_up_quietly(config, engine, caplog):
    announcer = Announcer(BotWith(FakeTextChannel()), config, engine)
    start(engine, T0, 1)
    announcer._progress_message = FlakyMessage(discord.Forbidden(_Resp(), "no edit"))

    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        run(announcer.update_progress(engine.snapshot(now=T0 + 60), force=True))

    assert "Missing permission to edit" in caplog.text


def test_a_failed_pin_does_not_matter(config, engine):
    """Pinning is a nicety; failing to pin must not lose the message."""
    channel = FakeTextChannel()

    async def send(content=None, **kwargs):
        return FlakyMessage()

    channel.send = send  # type: ignore[assignment]
    announcer = Announcer(BotWith(channel), config, engine)
    start(engine, T0, 1)

    run(announcer.update_progress(engine.snapshot(now=T0 + 60), force=True))

    assert engine.state.progress_message_id == 5


# ------------------------------------------------------------- monitoring


@pytest.fixture
def monitor(config, engine):
    voice = FakeVoiceChannel([FakeMember(1, "Alice")])
    bot = MonitorBot(voice)
    announcer = Announcer(BotWith(FakeTextChannel()), config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    monitor.voice_channel = lambda: voice  # type: ignore[method-assign]
    return monitor, voice


def test_going_blind_is_reported_after_a_while(config, engine, monitor, caplog):
    mon, _voice = monitor
    mon.voice_channel = lambda: None  # type: ignore[method-assign]

    assert run(mon.collect()) is None
    mon._blind_since = mon._blind_since - 60          # pretend it has been a minute
    with caplog.at_level(logging.ERROR, logger="hell.monitor"):
        run(mon.collect())

    assert "Cannot observe the target VC" in caplog.text
    assert mon.blind_seconds > 0


def test_regaining_sight_is_noted(config, engine, monitor, caplog):
    mon, _voice = monitor
    mon._blind_since = 0.0
    with caplog.at_level(logging.INFO, logger="hell.monitor"):
        run(mon.collect())
    assert "visible again" in caplog.text
    assert mon.blind_seconds == 0


def test_the_kick_cooldown_map_is_pruned(config, engine, monitor):
    mon, _voice = monitor
    mon._kick_attempts = dict.fromkeys(range(300), 0.0)   # ancient entries
    run(mon.kick_clankers([]))
    assert len(mon._kick_attempts) < 300


def test_the_heartbeat_summarises_the_run(config, engine, monitor, caplog):
    mon, _voice = monitor
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))
    mon._last_heartbeat = 0.0
    with caplog.at_level(logging.INFO, logger="hell.monitor"):
        mon._heartbeat(3)
    assert "Heartbeat" in caplog.text and "3 in VC" in caplog.text


def test_a_cancelled_event_is_announced_and_frozen(config, engine, monitor):
    mon, _voice = monitor
    start(engine, T0, 1)
    engine.tick(obs(T0 + 30, 1))
    event = engine.cancel(now=T0 + 31, by_user_id=7)

    run(mon.dispatch(event))

    assert engine.status is EventStatus.CANCELLED
    assert engine.state.final_saved


# --------------------------------------------------------------- recovery


def test_restart_finishes_the_stat_cards_of_a_finished_run(config, engine, monitor, caplog):
    mon, _voice = monitor
    start(engine, T0, 1)
    for i in range(1, 31):
        engine.tick(obs(T0 + i, 1))
    engine.tick(obs(T0 + 31))
    engine.tick(obs(T0 + 31 + GRACE))                  # FAILED

    with caplog.at_level(logging.INFO, logger="hell.monitor"):
        run(mon.resume_after_restart())

    assert "stat cards" in caplog.text


def test_restart_reannounces_a_milestone_that_never_posted(config, engine, monitor):
    mon, _voice = monitor
    channel = FakeTextChannel()
    mon.announcer = Announcer(BotWith(channel), config, engine)
    start(engine, T0, 1)
    engine.store.claim_milestone(engine.event_uid, 32, T0)   # claimed, never sent

    run(mon.resume_after_restart())

    posted = "\n".join(str(m.content) for m in channel.sent if m.content)
    assert "@everyone" in posted
    assert engine.pending_announcements() == []              # now marked as sent


def test_restart_cancels_a_roll_call_that_expired_offline(config, engine, monitor, caplog):
    from tests.test_alivecheck import FakeIO

    mon, _voice = monitor
    io = FakeIO()
    io.present = {1}
    mon.alive_checks.io = io
    start(engine, T0, 1)
    mon.alive_checks.bind(engine.event_uid, now=T0)
    engine.store.set_next_alive_check(engine.event_uid, T0)
    run(mon.alive_checks.tick(T0 + 1, [MagicMock(user_id=1, display_name="Alice")]))

    with caplog.at_level(logging.WARNING, logger="hell.monitor"):
        run(mon.resume_after_restart())

    assert "expired while offline" in caplog.text
    assert io.kicked == []                                  # nobody punished
    assert mon.alive_checks.pending is None


# ------------------------------------------------------------ small helpers


def test_cancelling_a_task_is_safe():
    async def scenario():
        async def forever():
            await asyncio.sleep(60)

        task = tasks.spawn(forever(), name="cancel-me")
        await tasks.cancel(task)
        assert task.cancelled() or task.done()
        await tasks.cancel(None)          # a no-op, never raises
        await tasks.cancel(task)          # already finished

    run(scenario())
    assert tasks.active() == 0


def test_paths_point_inside_the_project():
    base = paths.app_base()
    assert (base / "hell").is_dir()
    assert paths.env_path() == base / ".env"
    assert paths.data_dir().is_dir() and paths.logs_dir().is_dir()
    assert paths.log_file().name == "hellbot.log"
    assert paths.resolve("data/x.sqlite3") == base / "data" / "x.sqlite3"
    assert paths.resolve("/tmp/absolute.sqlite3").is_absolute()
    assert paths.is_frozen() is False
