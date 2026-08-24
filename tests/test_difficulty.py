"""Tests for Difficulties (0 to 4), dead checks, gambling, and difficulty setting/announcements."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hell.alivecheck import AliveCheckManager
from hell.difficulty import get_difficulty, get_difficulty_by_level, next_difficulty
from hell.models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from hell.reports import build_reports, render_report
from hell.timeutil import now_ts
from tests.conftest import T0, obs, start, users
from tests.test_alivecheck import FakeIO

HOUR = 3600.0
MINUTE = 60.0


# ------------------------------------------------------------- difficulty tiers


def test_difficulty_thresholds():
    assert get_difficulty(0.0).level == 0
    assert get_difficulty(31.9 * HOUR).level == 0
    assert get_difficulty(32.0 * HOUR).level == 1
    assert get_difficulty(63.9 * HOUR).level == 1
    assert get_difficulty(64.0 * HOUR).level == 2
    assert get_difficulty(95.9 * HOUR).level == 2
    assert get_difficulty(96.0 * HOUR).level == 3
    assert get_difficulty(127.9 * HOUR).level == 3
    assert get_difficulty(128.0 * HOUR).level == 4
    assert get_difficulty(160.0 * HOUR).level == 4


def test_difficulty_rules():
    d0 = get_difficulty_by_level(0)
    assert d0.min_check_hours == 1.0 and d0.max_check_hours == 6.0
    assert not d0.dead_checks_enabled and not d0.gamble_enabled

    d1 = get_difficulty_by_level(1)
    assert d1.min_check_hours == 1.0 and d1.max_check_hours == 5.0
    assert not d1.dead_checks_enabled and not d1.gamble_enabled

    d2 = get_difficulty_by_level(2)
    assert d2.min_check_hours == 1.0 and d2.max_check_hours == 4.0
    assert d2.dead_checks_enabled and d2.min_mute_seconds == 60 and d2.max_mute_seconds == 60
    assert not d2.gamble_enabled

    d3 = get_difficulty_by_level(3)
    assert d3.min_check_hours == 1.0 and d3.max_check_hours == 3.0
    assert d3.dead_checks_enabled and d3.min_mute_seconds == 60 and d3.max_mute_seconds == 300
    assert d3.gamble_enabled and d3.gamble_win_chance == 0.40
    assert d3.gamble_max_bet_hours == 1.0
    assert d3.gamble_hourly_limit == 2
    assert d3.gamble_win_multiplier == 1.5

    d4 = get_difficulty_by_level(4)
    assert d4.min_check_hours == 1.0 and d4.max_check_hours == 2.0
    assert d4.dead_checks_enabled and d4.min_mute_seconds == 300 and d4.max_mute_seconds == 900
    assert d4.gamble_enabled and d4.gamble_win_chance == 0.30
    assert d4.gamble_max_bet_hours == 2.0
    assert d4.gamble_hourly_limit == 3
    assert d4.gamble_win_multiplier == 2.5


def test_next_difficulty():
    assert next_difficulty(0.0).level == 1
    assert next_difficulty(32.0 * HOUR).level == 2
    assert next_difficulty(64.0 * HOUR).level == 3
    assert next_difficulty(96.0 * HOUR).level == 4
    assert next_difficulty(128.0 * HOUR) is None


def test_difficulty_override_and_clearing(engine):
    now = now_ts()
    start(engine, now, 1)
    # Natural difficulty at start is 0
    assert engine.current_difficulty(now).level == 0

    # Override to difficulty 3
    diff3 = engine.set_difficulty_override(3)
    assert diff3.level == 3
    assert engine.difficulty_override == 3
    assert engine.current_difficulty(now).level == 3

    # Clear override back to auto
    diff_auto = engine.set_difficulty_override(None)
    assert engine.difficulty_override is None
    assert diff_auto.level == 0


# ------------------------------------------------ alive check window scaling


def test_pick_delay_scales_with_difficulty(engine, store, config):
    io = FakeIO()
    manager = AliveCheckManager(config, store, io, rng=random.Random(42), engine=engine)
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)

    # Difficulty 0 (0h)
    for _ in range(50):
        d = manager.pick_delay(T0)
        assert 1 * HOUR <= d <= 6 * HOUR

    # Advance to Difficulty 1 (35h)
    engine.tick(obs(T0 + 35 * HOUR, 1, 2))
    for _ in range(50):
        d = manager.pick_delay(T0 + 35 * HOUR)
        assert 1 * HOUR <= d <= 5 * HOUR

    # Advance to Difficulty 2 (70h)
    engine.tick(obs(T0 + 70 * HOUR, 1, 2))
    for _ in range(50):
        d = manager.pick_delay(T0 + 70 * HOUR)
        assert 1 * HOUR <= d <= 4 * HOUR

    # Advance to Difficulty 3 (100h)
    engine.tick(obs(T0 + 100 * HOUR, 1, 2))
    for _ in range(50):
        d = manager.pick_delay(T0 + 100 * HOUR)
        assert 1 * HOUR <= d <= 3 * HOUR

    # Advance to Difficulty 4 (130h)
    engine.tick(obs(T0 + 130 * HOUR, 1, 2))
    for _ in range(50):
        d = manager.pick_delay(T0 + 130 * HOUR)
        assert 1 * HOUR <= d <= 2 * HOUR


# --------------------------------------------------------------- dead checks


def test_dead_check_flow(engine, store, config):
    async def flow():
        io = FakeIO()
        io.present = {1, 2, 3}
        manager = AliveCheckManager(config, store, io, rng=random.Random(10), engine=engine)
        start(engine, T0, 1, 2, 3)
        manager.bind(engine.event_uid, now=T0)

        # Advance engine to 70h (Difficulty 2)
        engine.tick(obs(T0 + 70 * HOUR, 1, 2, 3))

        # Force a dead check
        check = await manager.start(T0 + 70 * HOUR, users(1, 2, 3), force_type="dead")
        assert check is not None
        assert check.check_type == "dead"
        assert check.mute_duration == 60

        text, pinged = io.sent[0]
        assert "DEAD CHECK" in text
        assert pinged == [1, 2, 3]

        # User 1 falls for the trap and replies "Yes"
        assert manager.register_reply(1, "Yes", channel_id=999)
        assert 1 in check.trapped

        # Allow created background tasks to complete
        await asyncio.sleep(0.01)

        # User 2 and 3 stay silent (as instructed)
        result = await manager.resolve(T0 + 70 * HOUR + 5 * MINUTE, users(1, 2, 3))
        assert result.check_type == "dead"
        assert [p.user_id for p in result.trapped] == [1]
        assert result.kicked == []  # Silent users are NOT kicked
        assert io.kicked == []      # Nobody was kicked

        # Ensure mute was called for User 1
        assert any(m[0] == 1 and m[1] == 60 for m in io.muted)

        # Summary announcement
        summary = io.results[-1]
        assert "Dead check finished" in summary
        assert "<@1>" in summary

    asyncio.run(flow())


def test_dead_check_nobody_trapped(engine, store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(10), engine=engine)
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    engine.tick(obs(T0 + 70 * HOUR, 1, 2))

    asyncio.run(manager.start(T0 + 70 * HOUR, users(1, 2), force_type="dead"))

    # Both users stay silent
    result = asyncio.run(manager.resolve(T0 + 70 * HOUR + 5 * MINUTE, users(1, 2)))
    assert result.trapped == []
    assert result.kicked == []
    summary = io.results[-1]
    assert "Nobody fell for the dead check" in summary


def test_dead_check_persists_and_restores(engine, store, config):
    io = FakeIO()
    io.present = {1, 2}
    manager = AliveCheckManager(config, store, io, rng=random.Random(10), engine=engine)
    start(engine, T0, 1, 2)
    manager.bind(engine.event_uid, now=T0)
    engine.tick(obs(T0 + 100 * HOUR, 1, 2))

    check = asyncio.run(manager.start(T0 + 100 * HOUR, users(1, 2), force_type="dead"))
    assert check.check_type == "dead"
    manager.register_reply(1, "Yes", channel_id=999)

    # Restart manager
    reborn = AliveCheckManager(config, store, FakeIO(), rng=random.Random(10), engine=engine)
    reborn.bind(engine.event_uid, now=T0 + 100 * HOUR + 30)
    assert reborn.pending is not None
    assert reborn.pending.check_type == "dead"
    assert reborn.pending.trapped == {1}


# ----------------------------------------------------------------- gambling


def test_gambling_locked_at_low_difficulties(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 10 * HOUR, 1)
    engine.tick(obs(now, 1))  # Level 0

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    ok, msg = asyncio.run(cog._perform_gamble(user))
    assert not ok and "locked" in msg.lower()

    # Level 2 (65h)
    engine.reset()
    start(engine, now - 65 * HOUR, 1)
    engine.tick(obs(now, 1))
    ok, msg = asyncio.run(cog._perform_gamble(user))
    assert not ok and "locked" in msg.lower()


def test_gambling_unlocked_at_difficulty_3_and_4(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))  # Difficulty 3
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 3600.0, now)])

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    ok, msg = asyncio.run(cog._perform_gamble(user))
    assert ok  # Gamble successfully rolled
    assert "GAMBLE" in msg


def test_gambling_win_adds_bonus_seconds(engine, store, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 3600.0, now)])

    initial_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    # Force a win (random.random() -> 0.1) with default bet 0.25h (900s) on Diff 3 (1.5x multiplier -> +1350s)
    with patch("random.random", return_value=0.1):
        ok, msg = asyncio.run(cog._perform_gamble(user))

    assert ok
    assert "GAMBLE WON" in msg
    new_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)
    assert new_seconds == pytest.approx(initial_seconds + 1350.0)


def test_gambling_custom_bet_hours(engine, store, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 7200.0, now)])

    initial_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    # Bet 1.0 hour (3600s) on Diff 3 (1.5x multiplier -> +5400s)
    with patch("random.random", return_value=0.1):
        ok, msg = asyncio.run(cog._perform_gamble(user, hours=1.0))

    assert ok
    assert "GAMBLE WON" in msg
    new_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)
    assert new_seconds == pytest.approx(initial_seconds + 5400.0)


def test_gambling_exceeds_max_bet_hours(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 10000.0, now)])

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    # Diff 3 max bet is 1.0h; trying to bet 2.0h must fail
    ok, msg = asyncio.run(cog._perform_gamble(user, hours=2.0))
    assert not ok
    assert "at most" in msg.lower()


def test_gambling_hourly_limit_enforced(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 10000.0, now)])

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    # Bet 1
    with patch("random.random", return_value=0.1):
        ok1, _ = asyncio.run(cog._perform_gamble(user, hours=0.5))
    assert ok1

    # Simulate cooldown passing but still within the 1-hour window
    cog._gamble_cooldowns[1] = 0.0

    # Bet 2 (allowed on Diff 3: max 2/h)
    with patch("random.random", return_value=0.1):
        ok2, _ = asyncio.run(cog._perform_gamble(user, hours=0.5))
    assert ok2

    cog._gamble_cooldowns[1] = 0.0

    # Bet 3 (should be blocked by hourly limit)
    with patch("random.random", return_value=0.1):
        ok3, msg3 = asyncio.run(cog._perform_gamble(user, hours=0.5))
    assert not ok3
    assert "hourly" in msg3.lower()


def test_gambling_loss_applies_mute_and_timer_penalty(engine, store, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    io = FakeIO()
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    monitor.alive_checks.io = io
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 3600.0, now)])

    initial_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)
    assert initial_seconds >= 600.0

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    # Force a loss (random.random() -> 0.99)
    with patch("random.random", return_value=0.99):
        ok, msg = asyncio.run(cog._perform_gamble(user))

    assert ok
    assert "GAMBLE LOST" in msg
    assert "1 minute" in msg
    new_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)
    assert new_seconds == pytest.approx(initial_seconds - 900.0)  # -15m penalty
    assert any(m[0] == 1 and m[1] == 60 for m in io.muted)


def test_gambling_requires_minimum_time(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    # Give user 2 only 100 seconds (less than 900s requirement for 0.25h bet)
    engine.store.add_user_time(engine.event_uid, [(2, "Bob", 100.0, now)])

    user = MagicMock(id=2, display_name="Bob", mention="<@2>")
    ok, msg = asyncio.run(cog._perform_gamble(user))
    assert not ok
    assert "need at least" in msg.lower()


def test_gambling_cooldown_enforced(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 1)
    engine.tick(obs(now, 1))
    engine.store.add_user_time(engine.event_uid, [(1, "Alice", 3600.0, now)])

    user = MagicMock(id=1, display_name="Alice", mention="<@1>")
    ok, _ = asyncio.run(cog._perform_gamble(user))
    assert ok

    # Immediately try to gamble again
    ok2, msg2 = asyncio.run(cog._perform_gamble(user))
    assert not ok2
    assert "wait" in msg2.lower()


def test_dead_check_no_double_mute(engine, store, config):
    async def flow():
        io = FakeIO()
        io.present = {1, 2}
        manager = AliveCheckManager(config, store, io, rng=random.Random(10), engine=engine)
        start(engine, T0, 1, 2)
        manager.bind(engine.event_uid, now=T0)
        engine.tick(obs(T0 + 70 * HOUR, 1, 2))

        await manager.start(T0 + 70 * HOUR, users(1, 2), force_type="dead")
        manager.register_reply(1, "Yes", channel_id=999)
        await asyncio.sleep(0.01)
        # 1 mute recorded on reply
        assert len([m for m in io.muted if m[0] == 1]) == 1

        # Resolve
        await manager.resolve(T0 + 70 * HOUR + 5 * MINUTE, users(1, 2))
        # Still exactly 1 mute recorded (no double mute)
        assert len([m for m in io.muted if m[0] == 1]) == 1

    asyncio.run(flow())


def test_difficulty_embed(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 70 * HOUR, 1)
    engine.tick(obs(now, 1))

    embed = cog._build_difficulty_embed()
    assert embed.title is not None
    assert "DIFFICULTIES" in embed.title
    assert any("Level 2" in f.name for f in embed.fields)


def test_set_difficulty_handlers(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    start(engine, T0, 1)

    # Set to level 3
    ok, msg = asyncio.run(cog._handle_set_difficulty("3"))
    assert ok
    assert "Level 3" in msg
    assert engine.difficulty_override == 3

    # Invalid level
    ok_bad, msg_bad = asyncio.run(cog._handle_set_difficulty("99"))
    assert not ok_bad
    assert "between 0 and 4" in msg_bad

    # Reset to auto
    ok_auto, msg_auto = asyncio.run(cog._handle_set_difficulty("auto"))
    assert ok_auto
    assert "automatically" in msg_auto
    assert engine.difficulty_override is None


def test_announce_difficulty_handler(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    announcer.send = AsyncMock(return_value=MagicMock())
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    start(engine, T0, 1)

    ok, msg = asyncio.run(cog._handle_announce_difficulty(overview=False))
    assert ok
    assert "posted to" in msg
    assert announcer.send.call_count == 1

    ok_ov, _msg_ov = asyncio.run(cog._handle_announce_difficulty(overview=True))
    assert ok_ov
    assert announcer.send.call_count == 2


# ----------------------------------------------------- stat card present tense


def test_stat_card_present_tense_while_running():
    entries = [LeaderboardEntry(rank=1, user_id=1, display_name="Alice", seconds=3600.0)]
    records = [MilestoneRecord(hours=32, reached_ts=now_ts(), members=[ParticipantRef(1, "Alice")])]

    reports = build_reports(entries, records, status=EventStatus.RUNNING, event_elapsed=3600.0)
    card_text = render_report(reports[0])

    assert "SURVIVED SO FAR" in card_text
    assert "CURRENT RANK: TOP 1" in card_text
    assert "CLAIMED 1 REWARD SO FAR" in card_text
    assert "IN PROGRESS" in card_text
