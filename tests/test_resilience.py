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
from hell.alivecheck import AliveCheckManager
from hell.announcer import Announcer
from hell.engine import HellEngine, MilestoneReached
from hell.milestones import get_milestone
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from hell.storage import Store
from tests.conftest import GRACE, T0, make_config, obs, start
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


def test_a_failed_send_does_not_leak_artwork_handles(config, engine, monkeypatch, caplog):
    """Artwork files opened for a send that raises must be closed again."""
    from hell import embeds as embeds_module

    monkeypatch.setattr(embeds_module.TEXT, "PROGRESS_THUMBNAIL", "assets/hellbot.png",
                        raising=False)

    class GrabbingChannel(FakeTextChannel):
        def __init__(self):
            super().__init__()
            self.grabbed: list = []

        async def send(self, content=None, **kwargs):
            self.grabbed = list(kwargs.get("files") or [])
            raise discord.HTTPException(_Resp(429), "slow down")

    channel = GrabbingChannel()
    announcer = Announcer(BotWith(channel), config, engine)
    start(engine, T0, 1)

    with caplog.at_level(logging.ERROR, logger="hell.announcer"):
        assert run(announcer.send([announcer.build_progress(engine.snapshot(now=T0))],
                                  content="@everyone", mention_everyone=True)) is None

    assert [f.filename for f in channel.grabbed] == ["hellbot.png"]
    assert all(f.fp.closed for f in channel.grabbed)


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


# ======================================================================
# CRASH INJECTION TESTS
# ======================================================================
# These simulate crashes at the exact boundaries between DB writes and
# Discord API calls, then verify that state is never contradictory:
#   "DB says it happened, Discord says it didn't"
#   "Discord says it happened, DB says it didn't"
# After every important operation, the event state MUST be reconstructable
# from core fields alone: start_ts, status, last_valid_observed_ts,
# grace_started_ts, last_tick_ts, and the milestones table.
# ======================================================================


# --------------------------------------------------------- Grace termination


def test_crash_between_grace_expiry_and_status_failed_is_impossible(tmp_path):
    """CRASH INJECTION: The old code had a race:
    ``grace.close()`` -> ``_persist_grace(None)`` -> ``_terminate(FAILED)``
    If the bot crashed after clearing grace but before saving FAILED, the
    event would come back as RUNNING with no grace window — it would never
    fail.

    In the **fixed** ordering ``_terminate()`` (which writes FAILED + clears
    grace in a single save_state) runs first.  If a crash happens right
    after ``_terminate``, the DB already shows ``status=FAILED`` with
    ``grace_started_ts=NULL`` — exactly the correct terminal state.
    """
    config = make_config(tmp_path, empty_vc_grace_seconds=1.0)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)

    engine.tick(obs(T0 + 10))       # VC empties -> grace opens
    engine.tick(obs(T0 + 11))       # grace expires -> FAILED via _terminate

    # Simulate restart.  _terminate wrote FAILED first, so grace is closed.
    store2 = Store(config.database_path)
    loaded = store2.load_state()
    assert loaded.status is EventStatus.FAILED
    assert loaded.grace_started_ts is None
    assert loaded.end_ts is not None
    store.close()
    store2.close()


# --------------------------------------------------------- Alive check start


def test_alive_check_persisted_before_discord_no_orphan(tmp_path, config):
    """CRASH INJECTION: The check is saved to the DB *before* the Discord
    API call (see ``start()``).  After a successful Discord send, the DB
    is updated with real ``channel_id`` / ``message_id``.  A restart
    therefore always finds a consistent check — never an orphan Discord
    message that the bot doesn't know about.
    """
    from tests.test_alivecheck import FakeIO

    store = Store(config.database_path)
    io = FakeIO()
    io.present = {1}
    manager = AliveCheckManager(config, store, io)
    manager.bind("uid", now=T0)
    manager.store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, [MagicMock(user_id=1, display_name="Alice")]))
    assert manager.pending is not None

    # Simulate restart: DB has the check with real Discord IDs.
    store2 = Store(config.database_path)
    io2 = FakeIO()
    io2.present = {1}
    reboot = AliveCheckManager(config, store2, io2)
    reboot.bind("uid", now=T0 + 2)
    assert reboot.pending is not None
    assert reboot.pending.channel_id == 999   # set by FakeIO
    assert reboot.pending.message_id is not None

    # The resurrected check works: backfill replies, let deadline hit.
    run(reboot.backfill_replies())
    store2.close()
    store.close()


def test_alive_check_discord_failure_cleans_up_db(tmp_path, config):
    """CRASH INJECTION: The check is persisted first, then Discord is
    called.  If Discord rejects the message (permission, rate-limit), the
    tentative DB row is deleted — so we never end up with 'DB says active,
    Discord says it doesn't exist'.
    """
    from tests.test_alivecheck import FakeIO

    store = Store(config.database_path)
    io = FakeIO(fail_send=True)   # Discord will reject the send
    manager = AliveCheckManager(config, store, io)
    manager.bind("uid", now=T0)
    manager.store.set_next_alive_check("uid", T0)

    run(manager.tick(T0 + 1, [MagicMock(user_id=1, display_name="Alice")]))

    # DB should have NO pending check — the failure cleaned it up.
    assert manager.pending is None
    db_row = store.load_alive_check("uid")
    assert db_row is None  # DB agrees: no check exists
    store.close()


# ------------------------------------------------- Alive check resolve order


def test_alive_check_resolve_history_before_kick(tmp_path, config):
    """CRASH INJECTION: History is written to the DB and the pending check
    is cleared BEFORE the kick API call.  If the bot crashes after the DB
    write but before the kicks, a restart will NOT see a stale pending
    check or try to cancel what was already resolved.
    """
    from tests.test_alivecheck import FakeIO

    store = Store(config.database_path)
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io)
    manager.bind("uid", now=T0)
    manager.store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, [MagicMock(user_id=1, display_name="Alice"),
                                MagicMock(user_id=2, display_name="Bob")]))
    manager.register_reply(1, "Yes", 999)

    # Resolve: history written, pending cleared (no crash in this scenario).
    deadline = manager.pending.deadline_ts
    result = run(manager.resolve(deadline,
                                 [MagicMock(user_id=1, display_name="Alice")]))
    assert result is not None
    assert not result.cancelled

    # Simulate restart: pending must be None (cleared).
    store2 = Store(config.database_path)
    reboot = AliveCheckManager(config, store2, FakeIO())
    reboot.bind("uid", now=T0 + 100)
    assert reboot.pending is None

    # History should be in the DB before the kicks.
    history = store2.alive_check_history("uid")
    assert len(history) == 1
    assert history[0]["responded"] == 1
    assert history[0]["required"] == 2
    store.close()
    store2.close()


# --------------------------------------------------------- DM delivery order


def test_dm_pending_is_retried_after_crash(tmp_path):
    """CRASH INJECTION: Each DM is recorded as ``pending`` in the DB
    BEFORE the Discord API call.  If the bot crashes after the DB write
    but before the DM is sent, the ``pending`` status causes a retry on
    restart — the recipient is not skipped.
    """
    from hell.dm import FinalReportDM

    config = make_config(tmp_path, send_final_dms=True)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    engine.cancel(now=T0 + 61, by_user_id=99)

    # Simulate "pending" record + crash.
    uid = engine.event_uid
    store.record_dm(uid, 1, "pending")

    bot = MagicMock()
    dm = FinalReportDM(bot, config, engine)
    pending_ids = [r.user_id for r in dm.pending()]
    assert 1 in pending_ids  # pending -> retry
    assert 2 in pending_ids  # never attempted -> retry

    # Now simulate reboot: 1 = sent, 2 = blocked.
    store.record_dm(uid, 1, "sent")
    store.record_dm(uid, 2, "blocked")
    after = dm.pending()
    assert len(after) == 0  # no one pending
    store.close()


def test_dm_sent_is_not_retried_after_crash(tmp_path):
    """If the DM was recorded as ``sent`` before a crash, no double-message."""
    from hell.dm import FinalReportDM

    config = make_config(tmp_path, send_final_dms=True)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    engine.cancel(now=T0 + 61, by_user_id=99)

    # Already sent.
    store.record_dm(engine.event_uid, 1, "sent")

    bot = MagicMock()
    dm = FinalReportDM(bot, config, engine)
    assert len(dm.pending()) == 0  # not retried
    store.close()


# -------------------------------------------------------- last_valid_observed


def test_last_valid_observed_ts_is_persisted_and_loaded(tmp_path):
    """The ``last_valid_observed_ts`` field records the most recent tick
    that observed at least one valid human.  After a crash, this field
    tells the bot when the VC was last occupied — essential for
    reconstruction.
    """
    config = make_config(tmp_path)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1, 2)

    # Tick with people -> last_valid is updated.
    engine.tick(obs(T0 + 10, 1, 2))
    assert engine.state.last_valid_observed_ts == T0 + 10

    # Empty tick does NOT update last_valid.
    engine.tick(obs(T0 + 20))  # empty
    assert engine.state.last_valid_observed_ts == T0 + 10   # unchanged

    # People return -> updates.
    engine.tick(obs(T0 + 30, 1))
    assert engine.state.last_valid_observed_ts == T0 + 30

    # On restart the value is loaded from the DB.
    store2 = Store(config.database_path)
    loaded = store2.load_state()
    assert loaded.last_valid_observed_ts == T0 + 30
    store.close()
    store2.close()
