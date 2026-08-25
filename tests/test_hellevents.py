"""Tests for the Hell Events subsystem."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hell.announcer import Announcer
from hell.cog import HellCommands
from hell.hellevents import (
    HellEventManager,
    HellEventState,
    HellEventType,
    get_event_modifier,
)
from hell.models import ParticipantRef
from hell.monitor import VoiceMonitor
from hell.timeutil import now_ts
from tests.conftest import HOUR, T0, obs, start


def test_hellevents_modifiers_and_difficulty_scaling():
    # Double time
    m_dt0 = get_event_modifier(HellEventType.DOUBLE_TIME, difficulty_level=0)
    assert m_dt0.time_multiplier == 2.0
    assert m_dt0.duration_seconds == 300.0

    m_dt4 = get_event_modifier(HellEventType.DOUBLE_TIME, difficulty_level=4)
    assert m_dt4.time_multiplier == 2.5

    # Blood pact
    m_bp0 = get_event_modifier(HellEventType.BLOOD_PACT, difficulty_level=0)
    assert m_bp0.blood_pact_bonus_seconds == 300.0  # +5m

    m_bp3 = get_event_modifier(HellEventType.BLOOD_PACT, difficulty_level=3)
    assert m_bp3.blood_pact_bonus_seconds == 480.0  # +8m

    m_bp4 = get_event_modifier(HellEventType.BLOOD_PACT, difficulty_level=4)
    assert m_bp4.blood_pact_bonus_seconds == 600.0  # +10m

    # Inferno
    m_inf = get_event_modifier(HellEventType.INFERNO, difficulty_level=2)
    assert m_inf.duration_seconds == 600.0
    assert m_inf.inferno_min_check_seconds == 180.0

    # Blindness
    m_bl = get_event_modifier(HellEventType.BLINDNESS, difficulty_level=1)
    assert m_bl.duration_seconds == 600.0

    # Jackpot
    m_jp = get_event_modifier(HellEventType.JACKPOT, difficulty_level=3)
    assert m_jp.duration_seconds == 300.0
    assert m_jp.gamble_bonus_multiplier == 1.0


def test_hellevents_trigger_only_when_running(engine, config, store):
    mgr = HellEventManager(config, store, engine=engine)
    mgr.bind("event-1", now=T0)

    # When IDLE
    assert not mgr.is_due(T0)

    start(engine, T0, 100)
    # When RUNNING
    mgr.bind(engine.event_uid, now=T0)
    with patch.object(mgr, "next_event_ts", return_value=T0 - 10):
        assert mgr.is_due(T0)

    # When PAUSED
    engine.pause(now=T0)
    assert not mgr.is_due(T0)
    engine.resume(now=T0 + 10)

    # During empty-VC grace window
    engine.tick(obs(T0 + 20))  # empty obs opens grace
    assert engine.grace.is_open
    assert not mgr.is_due(T0 + 20)


def test_double_time_multiplier_on_user_credit(engine, config):
    now = now_ts()
    start(engine, now, 100)

    participants = [ParticipantRef(100, "Alice")]

    # Tick 5 seconds without double time
    engine.tick(obs(now + 5.0, 100))
    time_normal = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)
    assert time_normal == pytest.approx(5.0)

    # Start Double Time
    asyncio.run(engine.hell_events.start_event(HellEventType.DOUBLE_TIME, now + 5.0, participants))
    assert engine.hell_events.get_time_multiplier(now + 6.0) == 2.0

    # Tick 5 seconds during Double Time -> user gets +10 seconds
    engine.tick(obs(now + 10.0, 100))
    time_double = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)
    assert time_double == pytest.approx(15.0)

    # Global timer remains unaffected
    assert engine.elapsed(now + 10.0) == pytest.approx(10.0)

    # End Double Time
    asyncio.run(engine.hell_events.end_active_event(now + 305.0))
    assert engine.hell_events.get_time_multiplier(now + 306.0) == 1.0


def test_blood_pact_grants_instant_bonus(engine, config):
    now = now_ts()
    start(engine, now, 100, 200)

    participants = [ParticipantRef(100, "Alice"), ParticipantRef(200, "Bob")]

    # Initial tick
    engine.tick(obs(now + 5.0, 100, 200))

    # Blood Pact triggered
    started = asyncio.run(engine.hell_events.start_event(HellEventType.BLOOD_PACT, now + 5.0, participants))
    assert started is not None
    assert started.record.state is HellEventState.COMPLETED
    assert 100 in started.record.affected_users
    assert 200 in started.record.affected_users

    # Verify bonus was added
    time_alice = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)
    assert time_alice == pytest.approx(305.0)  # 5s + 300s bonus

    time_bob = next(e.seconds for e in engine.leaderboard() if e.user_id == 200)
    assert time_bob == pytest.approx(305.0)


def test_inferno_speeds_up_alive_checks(engine, config, store):
    from hell.alivecheck import AliveCheckManager
    from hell.aliveio import DiscordAliveCheckIO

    bot = MagicMock()
    io = DiscordAliveCheckIO(bot, config)
    checks = AliveCheckManager(config, store, io, engine=engine)

    now = now_ts()
    start(engine, now, 100)
    checks.bind(engine.event_uid, now=now)

    # Normal delay
    normal_delay = checks.pick_delay(now)
    assert normal_delay >= 3600.0

    # Start Inferno
    asyncio.run(engine.hell_events.start_event(HellEventType.INFERNO, now, [ParticipantRef(100, "Alice")]))
    assert engine.hell_events.is_inferno_active(now)

    # Delay during inferno is 3m - 6m (180s - 360s)
    inferno_delay = checks.pick_delay(now)
    assert 180.0 <= inferno_delay <= 360.0


def test_blindness_hides_progress_info(engine, config):
    now = now_ts()
    start(engine, now, 100)

    # Normal snapshot
    snap_normal = engine.snapshot(now=now + 10.0)
    assert not snap_normal.blindness_active

    # Start Blindness
    asyncio.run(engine.hell_events.start_event(HellEventType.BLINDNESS, now + 10.0, [ParticipantRef(100, "Alice")]))
    assert engine.hell_events.is_blindness_active(now + 15.0)

    snap_blind = engine.snapshot(now=now + 15.0)
    assert snap_blind.blindness_active

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    embed_blind = announcer.embeds.progress(snap_blind)

    # Verify remaining time & next milestone are hidden in embed fields
    rem_field = next(f for f in embed_blind.fields if "Time remaining" in f.name)
    assert "HIDDEN BY BLINDNESS" in rem_field.value

    next_field = next(f for f in embed_blind.fields if "Next milestone" in f.name)
    assert "HIDDEN BY BLINDNESS" in next_field.value

    # Main elapsed time is still displayed in description
    assert "0h 00m" in embed_blind.description


def test_jackpot_boosts_gambling_rewards(engine, config, store):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now - 97 * HOUR, 100)
    engine.tick(obs(now, 100))
    engine.store.add_user_time(engine.event_uid, [(100, "Alice", 7200.0, now)])

    initial_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)

    # Start Jackpot event
    asyncio.run(engine.hell_events.start_event(HellEventType.JACKPOT, now, [ParticipantRef(100, "Alice")]))
    assert engine.hell_events.get_gamble_modifier(now) == 1.0

    user = MagicMock(id=100, display_name="Alice", mention="<@100>")
    # Bet 1.0h (3600s) on Diff 3 (base 1.5x + 1.0x jackpot bonus = 2.5x multiplier -> +9000s reward)
    with patch("random.random", return_value=0.1):
        ok, msg = asyncio.run(cog._perform_gamble(user, hours=1.0))

    assert ok
    assert "GAMBLE WON" in msg
    assert "JACKPOT BONUS ACTIVE" in msg
    new_seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)
    assert new_seconds == pytest.approx(initial_seconds + 9000.0)


def test_hellevents_persistence_across_restart(engine, config, store):
    now = now_ts()
    start(engine, now, 100)

    # Start Double Time for 300s
    asyncio.run(engine.hell_events.start_event(HellEventType.DOUBLE_TIME, now, [ParticipantRef(100, "Alice")]))
    ev_id = engine.hell_events.active_event.id

    # Create new manager instance simulating bot restart
    mgr_recovered = HellEventManager(config, store, engine=engine)
    mgr_recovered.bind(engine.event_uid, now=now + 50.0)

    assert mgr_recovered.active_event is not None
    assert mgr_recovered.active_event.id == ev_id
    assert mgr_recovered.active_event.event_type is HellEventType.DOUBLE_TIME
    assert mgr_recovered.active_event.seconds_left(now + 50.0) == pytest.approx(250.0)


def test_hellevents_commands_and_handlers(engine, config):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    announcer.send = AsyncMock(return_value=MagicMock())
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now, 100)
    engine.tick(obs(now, 100))

    # View status
    embed = cog._build_hellevents_embed()
    assert embed.title is not None
    assert "HELL EVENTS" in embed.title

    # Host trigger event
    with patch.object(monitor, "collect", return_value=([ParticipantRef(100, "Alice")], [])):
        ok, msg = asyncio.run(cog._handle_trigger_hell_event("double_time"))
        assert ok
        assert "Double Time" in msg
        assert engine.hell_events.active_event is not None

        # Cannot trigger another event while active
        ok2, msg2 = asyncio.run(cog._handle_trigger_hell_event("blood_pact"))
        assert not ok2
        assert "already active" in msg2


def test_hellevents_pause_and_resume_shifts_schedule(engine, config):
    now = now_ts()
    start(engine, now, 100)
    engine.tick(obs(now, 100))

    # Start Double Time with end_ts = now + 300s
    asyncio.run(engine.hell_events.start_event(HellEventType.DOUBLE_TIME, now, [ParticipantRef(100, "Alice")]))
    orig_end = engine.hell_events.active_event.end_ts
    assert orig_end == pytest.approx(now + 300.0)

    # Pause for 600s
    engine.pause(now=now + 50.0)
    assert engine.is_paused

    # Resume at now + 650s (paused for 600s)
    engine.resume(now=now + 650.0)
    assert not engine.is_paused

    # Active event end_ts shifted forward by 600s
    assert engine.hell_events.active_event is not None
    assert engine.hell_events.active_event.end_ts == pytest.approx(orig_end + 600.0)
    assert engine.hell_events.active_event.seconds_left(now + 650.0) == pytest.approx(250.0)



# ============================================================== new event types


def test_time_vortex_halves_user_credit(engine, config):
    now = now_ts()
    start(engine, now, 100)

    participants = [ParticipantRef(100, "Alice")]
    engine.tick(obs(now + 5.0, 100))

    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.TIME_VORTEX, now + 5.0, participants)
    )
    assert started is not None
    assert engine.hell_events.get_time_multiplier(now + 6.0) == 0.5

    # Tick 10 seconds during the vortex -> user gets only +5 seconds
    engine.tick(obs(now + 15.0, 100))
    seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == 100)
    assert seconds == pytest.approx(10.0)

    # The global timer is unaffected
    assert engine.elapsed(now + 15.0) == pytest.approx(15.0)

    asyncio.run(engine.hell_events.end_active_event(now + 305.0))
    assert engine.hell_events.get_time_multiplier(now + 306.0) == 1.0


def test_blood_debt_charges_everyone_in_vc(engine, config):
    now = now_ts()
    start(engine, now, 100, 200)
    engine.tick(obs(now + 5.0, 100, 200))

    participants = [ParticipantRef(100, "Alice"), ParticipantRef(200, "Bob")]
    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.BLOOD_DEBT, now + 5.0, participants)
    )
    assert started is not None
    assert started.record.state is HellEventState.COMPLETED  # instant
    assert set(started.record.affected_users) == {100, 200}

    # Difficulty 0 penalty is 120s: everyone drops from 5s to 0 (clamped)
    for uid in (100, 200):
        seconds = next(e.seconds for e in engine.leaderboard() if e.user_id == uid)
        assert seconds == 0.0


def test_soul_cache_rewards_one_random_participant(engine, config):
    now = now_ts()
    start(engine, now, 100, 200)
    engine.tick(obs(now + 5.0, 100, 200))

    participants = [ParticipantRef(100, "Alice"), ParticipantRef(200, "Bob")]
    engine.hell_events.rng = random.Random(42)
    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.SOUL_CACHE, now + 5.0, participants)
    )
    assert started is not None
    assert started.record.state is HellEventState.COMPLETED  # instant

    winner_id = started.record.metadata["winner_id"]
    assert len(started.record.affected_users) == 1
    assert started.record.affected_users == [winner_id]

    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    # Winner: 5s of presence + 600s bonus (difficulty 0); loser keeps 5s.
    assert board[winner_id] == pytest.approx(605.0)
    other = 200 if winner_id == 100 else 100
    assert board[other] == pytest.approx(5.0)
    winner_name = "Alice" if winner_id == 100 else "Bob"
    assert winner_name in started.announcement_text
    assert "Soul Cache".upper() in started.announcement_text.upper()


def test_golden_hour_postpones_the_next_roll_call(engine, config, store):
    from hell.alivecheck import AliveCheckManager

    bot = MagicMock()
    checks = AliveCheckManager(config, store, bot, engine=engine)
    engine.hell_events.alive_checks = checks

    now = now_ts()
    start(engine, now, 100)
    checks.bind(engine.event_uid, now=now)
    original_due = checks.next_check_ts()
    assert original_due is not None

    started = asyncio.run(
        engine.hell_events.start_event(
            HellEventType.GOLDEN_HOUR, now, [ParticipantRef(100, "Alice")]
        )
    )
    assert started is not None
    assert started.record.state is HellEventState.COMPLETED  # instant
    # Difficulty 0 postpones by 1800s
    assert checks.next_check_ts() == pytest.approx(original_due + 1800.0)
    assert "postponed" in started.announcement_text.lower()


def test_golden_hour_is_skipped_while_a_check_is_pending(engine, config, store):
    from hell.alivecheck import AliveCheckManager, PendingCheck

    checks = AliveCheckManager(config, store, MagicMock(), engine=engine)
    engine.hell_events.alive_checks = checks

    now = now_ts()
    start(engine, now, 100)
    checks.bind(engine.event_uid, now=now)
    checks.pending = PendingCheck(
        check_id="x", started_ts=now, deadline_ts=now + 300, required={100: "Alice"}
    )

    started = asyncio.run(
        engine.hell_events.start_event(
            HellEventType.GOLDEN_HOUR, now, [ParticipantRef(100, "Alice")]
        )
    )
    assert started is None
    # The schedule moved on instead of retrying every second.
    assert engine.hell_events.next_event_ts() is not None
    assert engine.hell_events.next_event_ts() > now


def test_culling_triggers_an_immediate_roll_call(engine, config, store):
    from hell.alivecheck import AliveCheckManager

    io = MagicMock()
    io.send_check = AsyncMock(return_value=(555, 666))
    checks = AliveCheckManager(config, store, io, engine=engine)
    engine.hell_events.alive_checks = checks

    now = now_ts()
    start(engine, now, 100, 200)
    checks.bind(engine.event_uid, now=now)

    participants = [ParticipantRef(100, "Alice"), ParticipantRef(200, "Bob")]
    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.CULLING, now, participants)
    )
    assert started is not None
    assert started.record.state is HellEventState.COMPLETED  # instant
    assert checks.pending is not None
    assert checks.pending.check_type == "alive"
    assert set(checks.pending.required) == {100, 200}
    io.send_check.assert_awaited_once()
    assert "roll call" in started.announcement_text.lower()


def test_culling_is_skipped_when_a_check_is_already_running(engine, config, store):
    from hell.alivecheck import AliveCheckManager, PendingCheck

    io = MagicMock()
    checks = AliveCheckManager(config, store, io, engine=engine)
    engine.hell_events.alive_checks = checks

    now = now_ts()
    start(engine, now, 100)
    checks.bind(engine.event_uid, now=now)
    checks.pending = PendingCheck(
        check_id="x", started_ts=now, deadline_ts=now + 300, required={100: "Alice"}
    )

    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.CULLING, now, [ParticipantRef(100, "Alice")])
    )
    assert started is None
    io.send_check.assert_not_called()


# ============================================================ secret events


def test_secret_event_hides_then_reveals_itself(engine, config):
    now = now_ts()
    start(engine, now, 100)

    participants = [ParticipantRef(100, "Alice")]
    started = asyncio.run(
        engine.hell_events.start_event(
            HellEventType.DOUBLE_TIME, now, participants, secret=True
        )
    )
    assert started is not None
    assert started.secret is True
    assert started.record.metadata.get("secret") is True
    assert engine.hell_events.is_secret_active(now + 1.0)

    # The start announcement says *something* happened — never what.
    assert "DOUBLE TIME" not in started.announcement_text
    assert "SECRET" in started.announcement_text.upper()

    # The start embed hides name, duration and eligible participants.
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    embed = announcer.embeds.hell_event_start(started)
    text = str(embed.title) + "\n" + str(embed.description) + "\n" + "\n".join(
        f.name + ": " + f.value for f in embed.fields
    )
    assert "DOUBLE TIME" not in text.upper()
    assert "???" in str(embed.title)
    assert not any("Duration" in f.name for f in embed.fields)
    assert not any("Eligible" in f.name for f in embed.fields)

    # While it runs, the /hell hellevents card must not leak the name either.
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    status_embed = cog._build_hellevents_embed()
    status_text = "\n".join(f.name + ": " + f.value for f in status_embed.fields)
    # "Double Time" may appear in the static rules list — but never as the ACTIVE event.
    assert "ACTIVE: Double Time" not in status_text
    assert "Secret" in status_text

    # When it ends, the veil lifts and the event is revealed by name.
    ended = asyncio.run(engine.hell_events.end_active_event(now + 301.0))
    assert ended is not None
    assert "Double Time" in ended.announcement_text
    end_text = str(announcer.embeds.hell_event_end(ended).title)
    assert "Double Time".upper() in end_text.upper()
    assert engine.hell_events.is_secret_active(now + 301.0) is False


def test_secret_events_only_apply_to_timed_events(engine, config):
    now = now_ts()
    start(engine, now, 100)
    participants = [ParticipantRef(100, "Alice")]

    # An instant event cannot stay hidden — the secret flag falls back to False.
    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.BLOOD_PACT, now, participants, secret=True)
    )
    assert started is not None
    assert started.secret is False
    assert "secret" not in started.record.metadata


def test_tick_rolls_secret_events_from_the_timed_pool(engine, config, store):
    now = now_ts()
    start(engine, now, 100)
    mgr = engine.hell_events
    store.set_next_hell_event(engine.event_uid, now - 1)

    rng = MagicMock()
    rng.random.return_value = 0.05  # < SECRET_EVENT_CHANCE -> secret
    rng.choice.side_effect = [HellEventType.BLINDNESS]
    mgr.rng = rng

    outcomes = asyncio.run(mgr.tick(now, [ParticipantRef(100, "Alice")]))
    assert len(outcomes) == 1
    started = outcomes[0]
    assert isinstance(started, type(outcomes[0]))
    assert getattr(started, "secret", False) is True
    assert started.record.event_type is HellEventType.BLINDNESS
    assert started.record.metadata.get("secret") is True


def test_trigger_secret_hell_event_command(engine, config):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    announcer.send = AsyncMock(return_value=MagicMock())
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    now = now_ts()
    start(engine, now, 100)
    engine.tick(obs(now, 100))

    with patch.object(monitor, "collect", return_value=([ParticipantRef(100, "Alice")], [])):
        ok, msg = asyncio.run(cog._handle_trigger_hell_event("secret"))
        assert ok
        assert "secret" in msg.lower()
        assert engine.hell_events.active_event is not None
        assert engine.hell_events.is_secret_active(now + 1.0)

        # The public status card must not leak what it is.
        status_text = "\n".join(f.name + ": " + f.value for f in cog._build_hellevents_embed().fields)
        assert "???" in status_text


# ============================================ announcing in the VC text chat


class _ChanBot:
    """A bot that can see the announcement channel and the VC text chat."""

    def __init__(self, text, voice):
        self._text = text
        self._voice = voice

    def get_channel(self, cid):
        if cid == self._text.id:
            return self._text
        if cid == self._voice.id:
            return self._voice
        return None

    async def fetch_channel(self, cid):
        return self.get_channel(cid)


def test_hell_event_announcements_are_echoed_to_the_vc(engine, config):
    from tests.test_integration import FakeTextChannel
    from tests.test_monitor import FakeMember, FakeVoiceChannel

    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(100, "Alice"), FakeMember(200, "Bob")])
    bot = _ChanBot(text, voice)
    announcer = Announcer(bot, config, engine)
    engine.hell_events.announcer = announcer

    now = now_ts()
    start(engine, now, 100, 200)

    participants = (ParticipantRef(100, "Alice"), ParticipantRef(200, "Bob"))
    started = asyncio.run(
        engine.hell_events.start_event(HellEventType.DOUBLE_TIME, now, list(participants))
    )
    assert started is not None

    # Start: posted in the announcement channel AND echoed to the VC with pings.
    assert len(text.sent) == 1
    assert len(voice.sent) == 1
    vc_post = voice.sent[0]
    assert "<@100>" in vc_post["content"] and "<@200>" in vc_post["content"]
    assert vc_post["allowed_mentions"].users is True
    ann_post = text.sent[0]
    assert not ann_post.content  # no ping in the announcement channel copy

    ended = asyncio.run(engine.hell_events.end_active_event(now + 301.0))
    assert ended is not None

    # End: posted to both places, but without pinging anyone.
    assert len(text.sent) == 2
    assert len(voice.sent) == 2
    assert not voice.sent[1]["content"]


def test_hell_event_echo_skipped_when_vc_is_the_announcement_channel(engine, config):
    from tests.test_integration import FakeTextChannel
    from tests.test_monitor import FakeMember, FakeVoiceChannel

    # The VC text chat is also configured as the announcement channel.
    voice = FakeVoiceChannel([FakeMember(100, "Alice")])
    text = FakeTextChannel(voice.id)
    bot = _ChanBot(text, voice)
    announcer = Announcer(bot, config, engine)
    engine.hell_events.announcer = announcer

    now = now_ts()
    start(engine, now, 100)
    engine.state.announce_channel_id = voice.id  # announce straight into the VC chat

    started = asyncio.run(
        engine.hell_events.start_event(
            HellEventType.DOUBLE_TIME, now, [ParticipantRef(100, "Alice")]
        )
    )
    assert started is not None
    # One message total — not the same post twice in the same channel.
    assert len(text.sent) == 1
    assert len(voice.sent) == 0
