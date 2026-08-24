"""Alive checks ("🚨 ARE YOU ALIVE?") — scheduling, replies, kicks, recovery."""

from __future__ import annotations

import asyncio
import random

import pytest

from hell.alivecheck import CHECK_TEXT, AliveCheckManager, is_valid_reply
from hell.models import EventStatus
from tests.conftest import T0, make_config, obs, start, users

HOUR = 3600.0
MINUTE = 60.0


class FakeIO:
    """Records what would have been sent to Discord."""

    def __init__(self, *, fail_send: bool = False):
        self.sent: list[tuple[str, list[int]]] = []
        self.results: list[str] = []
        self.kicked: list[list[int]] = []
        self.muted: list[tuple[int, int, str]] = []  # (user_id, duration_seconds, reason)
        self.present: set[int] = set()          # who is actually in the VC
        self.offline_replies: set[int] = set()  # answers found after a restart
        self.fail_send = fail_send

    async def send_check(self, text, user_ids):
        if self.fail_send:
            return None
        self.sent.append((text, list(user_ids)))
        return (999, 1000 + len(self.sent))

    async def send_result(self, text):
        self.results.append(text)

    async def kick(self, user_ids, reason):
        removed = [u for u in user_ids if u in self.present]
        self.kicked.append(removed)
        self.present -= set(removed)
        return removed

    async def mute(self, user_id, duration_seconds, reason):
        self.muted.append((user_id, duration_seconds, reason))
        return True

    async def replies_since(self, channel_id, message_id, user_ids):
        return set(self.offline_replies) & set(user_ids)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def alive(store, config):
    io = FakeIO()
    manager = AliveCheckManager(config, store, io, rng=random.Random(1234))
    manager.bind("uid", now=T0)
    return manager, io


# ------------------------------------------------------------- reply parsing

@pytest.mark.parametrize("text", ["Yes", "yes", "YES", " Yes ", "yes!", "Yes."])
def test_accepted_replies(text):
    assert is_valid_reply(text)


@pytest.mark.parametrize("text", ["y", "yeah", "yes i am", "no", "", "si", "yesss"])
def test_rejected_replies(text):
    assert not is_valid_reply(text)


def test_strict_mode_requires_the_exact_word():
    assert is_valid_reply("Yes", strict=True)
    assert not is_valid_reply("yes", strict=True)
    assert not is_valid_reply("Yes!", strict=True)


# ---------------------------------------------------------------- scheduling

def test_delay_is_always_inside_the_configured_window(alive):
    manager, _io = alive
    for _ in range(500):
        delay = manager.pick_delay()
        assert 1 * HOUR <= delay <= 6 * HOUR


def test_delays_are_actually_random(alive):
    manager, _ = alive
    delays = {round(manager.pick_delay()) for _ in range(50)}
    assert len(delays) > 40


def test_first_check_is_scheduled_on_bind(alive, store):
    when = store.get_next_alive_check("uid")
    assert when is not None
    assert T0 + 1 * HOUR <= when <= T0 + 6 * HOUR


def test_check_is_not_due_before_its_time(alive):
    manager, io = alive
    run(manager.tick(T0 + 30 * MINUTE, users(1, 2)))
    assert io.sent == []
    assert manager.pending is None


def test_check_fires_when_due(alive, store):
    manager, io = alive
    store.set_next_alive_check("uid", T0 + 2 * HOUR)
    run(manager.tick(T0 + 2 * HOUR, users(1, 2, 3)))
    assert manager.pending is not None
    text, pinged = io.sent[0]
    assert text.startswith(CHECK_TEXT)          # exact required wording, first line
    assert pinged == [1, 2, 3]


def test_no_check_is_started_when_the_vc_is_empty(alive, store):
    manager, io = alive
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, []))
    assert manager.pending is None and io.sent == []


def test_next_check_is_rescheduled_after_each_one(alive, store):
    manager, io = alive
    io.present = {1}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1)))
    first_deadline = manager.pending.deadline_ts
    run(manager.tick(first_deadline, users(1)))
    nxt = store.get_next_alive_check("uid")
    assert nxt >= first_deadline + 1 * HOUR


def test_failed_send_reschedules_instead_of_kicking(store, config):
    io = FakeIO(fail_send=True)
    manager = AliveCheckManager(config, store, io, rng=random.Random(7))
    manager.bind("uid", now=T0)
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    assert manager.pending is None
    assert io.kicked == []
    assert store.get_next_alive_check("uid") > T0 + 1


# ------------------------------------------------------------------ replies

def test_only_silent_users_are_kicked(alive, store):
    manager, io = alive
    io.present = {1, 2, 3}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2, 3)))

    assert manager.register_reply(1, "Yes", channel_id=999)
    assert manager.register_reply(2, "yes", channel_id=999)
    assert not manager.register_reply(3, "nah", channel_id=999)

    result = run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2, 3)))
    assert [p.user_id for p in result.responded] == [1, 2]
    assert [p.user_id for p in result.kicked] == [3]
    assert io.kicked == [[3]]
    assert manager.pending is None


def test_reply_in_another_channel_does_not_count(alive, store):
    manager, io = alive
    io.present = {1}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1)))
    assert not manager.register_reply(1, "Yes", channel_id=12345)
    assert manager.register_reply(1, "Yes", channel_id=999)


def test_duplicate_replies_are_ignored(alive, store):
    manager, io = alive
    io.present = {1}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1)))
    assert manager.register_reply(1, "Yes", 999)
    assert not manager.register_reply(1, "Yes", 999)


def test_users_who_join_mid_check_are_not_required(alive, store):
    manager, io = alive
    io.present = {1, 2}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1)))          # only user 1 was in the VC
    assert not manager.register_reply(2, "Yes", 999)  # user 2 joined later
    manager.register_reply(1, "Yes", 999)
    result = run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    assert result.kicked == []                    # the newcomer is not punished


def test_user_who_left_before_the_deadline_is_not_kicked(alive, store):
    manager, io = alive
    io.present = {1}                              # user 2 already left
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    manager.register_reply(1, "Yes", 999)
    result = run(manager.tick(T0 + 1 + 5 * MINUTE, users(1)))
    assert result.kicked == []
    assert [p.user_id for p in result.left_early] == [2]


def test_result_message_explains_the_consequences(alive, store):
    manager, io = alive
    io.present = {1, 2}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    manager.register_reply(1, "Yes", 999)
    run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    summary = io.results[-1]
    assert "Disconnected" in summary and "<@2>" in summary
    assert "leaderboard time is untouched" in summary


# --------------------------------------------------------------- persistence

def test_pending_check_survives_a_restart(store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(3))
    manager.bind("uid", now=T0)
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    manager.register_reply(1, "Yes", 999)

    reborn = AliveCheckManager(config, store, FakeIO(), rng=random.Random(3))
    reborn.bind("uid", now=T0 + 60)
    assert reborn.pending is not None
    assert reborn.pending.responded == {1}
    assert reborn.pending.missing == {2}


def test_replies_sent_while_offline_are_recovered(store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(3))
    manager.bind("uid", now=T0)
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))

    io2 = FakeIO()
    io2.present = {1, 2}
    io2.offline_replies = {1, 2}
    reborn = AliveCheckManager(config, store, io2, rng=random.Random(3))
    reborn.bind("uid", now=T0 + 60)
    assert run(reborn.backfill_replies()) == 2
    result = run(reborn.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    assert result.kicked == []


def test_check_expired_while_offline_is_cancelled_not_enforced(store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(3))
    manager.bind("uid", now=T0)
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))

    io2 = FakeIO()
    io2.present = {1, 2}
    reborn = AliveCheckManager(config, store, io2, rng=random.Random(3))
    reborn.bind("uid", now=T0 + 2 * HOUR)
    result = run(reborn.cancel(T0 + 2 * HOUR, "the bot restarted while it was running"))
    assert result.cancelled and io2.kicked == []
    assert "cancelled" in io2.results[-1].lower()
    assert reborn.pending is None


def test_history_is_recorded(alive, store):
    manager, io = alive
    io.present = {1, 2}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    manager.register_reply(1, "Yes", 999)
    run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    history = store.alive_check_history("uid")
    assert len(history) == 1
    assert history[0]["required"] == 2 and history[0]["responded"] == 1
    assert history[0]["kicked"] == [2]


# ------------------------------------------------- interaction with the event

def test_kicked_user_keeps_leaderboard_time_and_can_return(engine, store, config):
    """Being disconnected costs presence, never accumulated time."""
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(11))
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    store.set_next_alive_check(engine.event_uid, T0)

    for i in range(1, 61):                      # a minute together in the VC
        engine.tick(obs(T0 + i, 1, 2))
    run(manager.tick(T0 + 61, users(1, 2)))
    manager.register_reply(1, "Yes", 999)
    result = run(manager.tick(T0 + 61 + 5 * MINUTE, users(1, 2)))
    assert [p.user_id for p in result.kicked] == [2]

    before = {e.user_id: e.seconds for e in engine.leaderboard()}
    t = T0 + 61 + 5 * MINUTE
    for i in range(1, 31):                      # user 2 is out of the VC
        engine.tick(obs(t + i, 1))
    after_kick = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert after_kick[2] == pytest.approx(before[2])        # nothing lost
    assert engine.status is EventStatus.RUNNING             # event unaffected

    t += 30
    for i in range(1, 31):                      # user 2 rejoins immediately
        engine.tick(obs(t + i, 1, 2))
    resumed = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert resumed[2] > after_kick[2]                        # tracking resumed


def test_event_survives_a_kick_while_someone_remains(engine, store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(5))
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    store.set_next_alive_check(engine.event_uid, T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    manager.register_reply(1, "Yes", 999)
    run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))

    engine.tick(obs(T0 + 2 + 5 * MINUTE, 1))    # only user 1 left in the VC
    assert engine.status is EventStatus.RUNNING


def test_event_fails_if_everyone_ignores_the_check(engine, store, config):
    """Nobody answers -> everyone is disconnected -> the VC empties -> 2-min recovery grace -> FAILED."""
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(5))
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    store.set_next_alive_check(engine.event_uid, T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    result = run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    assert sorted(p.user_id for p in result.kicked) == [1, 2]
    assert result.emptied_vc

    t_empty = T0 + 2 + 5 * MINUTE
    opened = engine.tick(obs(t_empty))
    assert opened and opened[0].seconds == 120.0  # 2-minute recovery grace period

    # After normal 15s grace, still RUNNING because of 2-minute recovery period
    engine.tick(obs(t_empty + 15.0))
    assert engine.status is EventStatus.RUNNING

    # After 120s without anyone returning, the event FAILS
    expired = engine.tick(obs(t_empty + 120.0))
    assert engine.status is EventStatus.FAILED
    assert expired and type(expired[0]).__name__ == "EventFailed"


def test_alive_check_recovery_grace_recovers_if_someone_returns(engine, store, config):
    """Nobody answers -> disconnected -> 2-min recovery grace starts -> user returns at 60s -> run continues."""
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(5))
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    store.set_next_alive_check(engine.event_uid, T0)
    run(manager.tick(T0 + 1, users(1, 2)))
    result = run(manager.tick(T0 + 1 + 5 * MINUTE, users(1, 2)))
    assert result.emptied_vc

    t_empty = T0 + 2 + 5 * MINUTE
    opened = engine.tick(obs(t_empty))
    assert opened and opened[0].seconds == 120.0

    # User 1 rejoins at 60s (within 2-min recovery period)
    recovered = engine.tick(obs(t_empty + 60.0, 1))
    assert engine.status is EventStatus.RUNNING
    assert recovered and type(recovered[0]).__name__ == "GraceRecovered"

    # Future regular empty-VC uses the normal 15s grace period
    t_normal_empty = t_empty + 100.0
    normal_opened = engine.tick(obs(t_normal_empty))
    assert normal_opened and normal_opened[0].seconds == 15.0


def test_disabled_checks_never_fire(tmp_path, store):
    config = make_config(tmp_path, alive_check_enabled=False)
    io = FakeIO()
    manager = AliveCheckManager(config, store, io, rng=random.Random(2))
    manager.bind("uid", now=T0)
    assert run(manager.tick(T0 + 10 * HOUR, users(1))) is None
    assert io.sent == []


def test_custom_window_is_respected(tmp_path, store):
    config = make_config(tmp_path, alive_check_min_hours=0.5, alive_check_max_hours=0.75)
    manager = AliveCheckManager(config, store, FakeIO(), rng=random.Random(9))
    for _ in range(100):
        assert 1800 <= manager.pick_delay() <= 2700


def test_status_line_never_reveals_the_next_check_time(alive, store):
    manager, io = alive
    line = manager.status_line(T0)
    assert "random" in line.lower()
    assert str(int(store.get_next_alive_check("uid"))) not in line
    io.present = {1}
    store.set_next_alive_check("uid", T0)
    run(manager.tick(T0 + 1, users(1)))
    assert "0/1 answered" in manager.status_line(T0 + 2).replace("**", "")


def test_manager_rebinds_itself_to_a_new_event(config, store, engine, monkeypatch):
    """An unbound manager used to mean 'no roll calls, ever, silently'."""
    from hell.announcer import Announcer
    from hell.monitor import VoiceMonitor
    from hell.timeutil import now_ts
    from tests.test_monitor import FakeBot, FakeMember, FakeVoiceChannel

    voice = FakeVoiceChannel([FakeMember(1, "Alice")])
    bot = FakeBot(voice)
    monitor = VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)
    assert monitor.alive_checks.bound_uid is None      # no event at construction

    start(engine, now_ts(), 1)                          # started without the command
    monitor._ensure_alive_checks_bound(now_ts())

    assert monitor.alive_checks.bound_uid == engine.event_uid
    assert store.get_next_alive_check(engine.event_uid) is not None


def test_a_raising_send_check_never_leaves_a_phantom_check(store, config):
    """Regression: an *exception* from send_check (network error) used to
    leave the tentative DB row and `pending` in place — a check that had
    never been posted would later 'resolve' and kick people who never saw
    a roll call.  Exceptions must clean up exactly like a `None` result."""
    from unittest.mock import MagicMock

    class ExplodingIO(FakeIO):
        async def send_check(self, text, user_ids):
            raise TimeoutError("gateway went away")

    io = ExplodingIO()
    manager = AliveCheckManager(config, store, io, rng=random.Random(1))
    manager.bind("uid", now=T0)
    manager.store.set_next_alive_check("uid", T0)

    run(manager.tick(T0 + 1, [MagicMock(user_id=1, display_name="Alice")]))

    assert manager.pending is None                     # no phantom check
    assert store.load_alive_check("uid") is None       # DB agrees
    assert store.get_next_alive_check("uid") is not None  # rescheduled
