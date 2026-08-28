"""Engine behaviour: timer, failure, restarts, completion, edge cases."""

from __future__ import annotations

import pytest

from hell.engine import (
    EventCompleted,
    EventFailed,
    GraceStarted,
    HellEngine,
    MilestoneReached,
    StartError,
)
from hell.milestones import TOTAL_SECONDS
from hell.models import EventStatus
from tests.conftest import GRACE, HOUR, T0, empty_out, make_config, obs, start

# ------------------------------------------------------------------- start

def test_start_sets_running_state_and_timestamp(engine):
    state = start(engine, T0, 1, 2)
    assert state.status is EventStatus.RUNNING
    assert state.start_ts == T0
    assert engine.elapsed(T0 + 90) == 90


def test_second_start_is_rejected(engine):
    start(engine, T0, 1)
    with pytest.raises(StartError):
        start(engine, T0 + 10, 1)


def test_start_after_a_finished_run_creates_a_new_event(engine):
    start(engine, T0, 1)
    empty_out(engine, T0 + 1)  # empty for the whole grace window -> FAILED
    first_uid = engine.event_uid
    state = start(engine, T0 + 100, 1)
    assert state.status is EventStatus.RUNNING
    assert state.event_uid != first_uid


# ---------------------------------------------------------------- failing

def test_empty_vc_fails_after_the_grace_period(engine):
    start(engine, T0, 1)
    opened, expired = empty_out(engine, T0 + 1)
    assert isinstance(opened[0], GraceStarted)      # warning first, no instant death
    assert engine.status is EventStatus.RUNNING or isinstance(expired[0], EventFailed)
    assert isinstance(expired[0], EventFailed)
    assert engine.status is EventStatus.FAILED
    # Further ticks are inert, even with people back in the VC.
    assert engine.tick(obs(T0 + 2, 1, 2)) == []
    assert engine.status is EventStatus.FAILED


def test_elapsed_is_frozen_after_failure(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 30, 1))
    empty_out(engine, T0 + 31)
    frozen = engine.elapsed(T0 + 31)
    assert engine.elapsed(T0 + 10_000) == pytest.approx(frozen)


def test_one_user_leaving_while_others_remain_does_not_fail(engine):
    start(engine, T0, 1, 2, 3)
    engine.tick(obs(T0 + 1, 1, 2, 3))
    engine.tick(obs(T0 + 2, 2, 3))
    engine.tick(obs(T0 + 3, 3))
    assert engine.status is EventStatus.RUNNING


def test_only_bots_or_clankers_cannot_save_the_event(engine):
    """The monitor filters them out, so the engine simply sees an empty VC."""
    start(engine, T0, 1)
    _opened, expired = empty_out(engine, T0 + 1)
    assert isinstance(expired[0], EventFailed)


def test_failure_freezes_final_leaderboard(engine, store):
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    empty_out(engine, T0 + 61)
    assert engine.state.final_saved
    saved = store.get_final_leaderboard(engine.event_uid)
    assert saved and saved[0].user_id == 1
    assert saved[0].seconds == pytest.approx(60, abs=1.5)


# ------------------------------------------------------- time accumulation

def test_user_time_accumulates_only_while_present(engine):
    start(engine, T0, 1, 2)
    for i in range(1, 11):
        engine.tick(obs(T0 + i, 1, 2))
    for i in range(11, 21):
        engine.tick(obs(T0 + i, 1))  # user 2 left
    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert board[1] == pytest.approx(20, abs=1.5)
    assert board[2] == pytest.approx(10, abs=1.5)


def test_leaving_and_returning_continues_accumulating(engine):
    start(engine, T0, 1, 2)
    for i in range(1, 6):
        engine.tick(obs(T0 + i, 1, 2))
    for i in range(6, 16):
        engine.tick(obs(T0 + i, 1))
    for i in range(16, 21):
        engine.tick(obs(T0 + i, 1, 2))
    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert board[2] == pytest.approx(10, abs=1.5)  # 5 + 5, not reset


def test_downtime_is_not_credited(engine, store):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 1, 1))
    engine.tick(obs(T0 + 3600, 1))  # bot was offline for an hour
    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert board[1] <= 6  # capped by max_tick_credit
    assert store.get_unverified_seconds(engine.event_uid) > 3500


def test_no_time_is_awarded_after_the_event_ends(engine):
    start(engine, T0, 1)
    for i in range(1, 11):
        engine.tick(obs(T0 + i, 1))
    empty_out(engine, T0 + 11)  # fail
    before = engine.leaderboard()[0].seconds
    for i in range(12 + int(GRACE), 40 + int(GRACE)):
        engine.tick(obs(T0 + i, 1))
    assert engine.leaderboard()[0].seconds == pytest.approx(before)


# ------------------------------------------------------------- milestones

def test_milestone_triggers_once_with_member_snapshot(engine):
    start(engine, T0, 1)
    assert engine.tick(obs(T0 + 32 * HOUR - 1, 1)) == []
    events = engine.tick(obs(T0 + 32 * HOUR, 1, 2))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, MilestoneReached)
    assert ev.milestone.hours == 32
    assert {m.user_id for m in ev.members} == {1, 2}
    assert ev.reached_ts == T0 + 32 * HOUR
    # never again
    assert engine.tick(obs(T0 + 32 * HOUR + 1, 1)) == []


def test_user_joining_exactly_at_the_milestone_is_eligible(engine):
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + 32 * HOUR, 1, 7))
    assert {m.user_id for m in events[0].members} == {1, 7}


def test_user_leaving_exactly_at_the_milestone_is_not_eligible(engine):
    start(engine, T0, 1, 7)
    events = engine.tick(obs(T0 + 32 * HOUR, 1))
    assert {m.user_id for m in events[0].members} == {1}


def test_milestones_are_never_duplicated_after_a_restart(engine, store, config):
    start(engine, T0, 1)
    first = engine.tick(obs(T0 + 32 * HOUR, 1))
    assert len(first) == 1
    reborn = HellEngine(store, config)  # simulate process restart
    assert reborn.status is EventStatus.RUNNING
    assert reborn.tick(obs(T0 + 32 * HOUR + 0.5, 1)) == []


def test_milestones_missed_during_downtime_fire_once_and_are_flagged_late(engine):
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + 70 * HOUR, 1, 2))
    hours = [e.milestone.hours for e in events if isinstance(e, MilestoneReached)]
    assert hours == [32, 64]
    assert all(e.late for e in events)


def test_milestone_is_not_awarded_while_the_vc_is_empty(engine):
    """Nobody in the VC = nobody who could claim it, so it waits (or dies)."""
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + 32 * HOUR))          # empty at the exact mark
    assert isinstance(events[0], GraceStarted)
    assert engine.store.triggered_milestone_hours(engine.event_uid) == set()
    expired = engine.tick(obs(T0 + 32 * HOUR + GRACE))  # nobody came back
    assert isinstance(expired[0], EventFailed)
    assert engine.store.triggered_milestone_hours(engine.event_uid) == set()


def test_milestone_missed_during_grace_fires_when_people_return(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 32 * HOUR))                    # empty exactly at 32h
    events = engine.tick(obs(T0 + 32 * HOUR + 5, 1, 2))  # rescued in time
    kinds = [type(e).__name__ for e in events]
    assert "GraceRecovered" in kinds and "MilestoneReached" in kinds
    milestone = next(e for e in events if isinstance(e, MilestoneReached))
    assert {m.user_id for m in milestone.members} == {1, 2}   # the rescuers claim it
    assert engine.status is EventStatus.RUNNING


def test_milestone_is_based_on_global_timer_not_user_time(engine):
    start(engine, T0, 1)
    # User 2 only shows up right before the 32h mark and still triggers nothing
    # early; the milestone lands exactly on the global clock.
    assert engine.tick(obs(T0 + 31 * HOUR, 1, 2)) == []
    events = engine.tick(obs(T0 + 32 * HOUR, 1, 2))
    assert events and events[0].milestone.hours == 32


# ------------------------------------------------------------- completion

def test_completion_at_160_hours(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 159 * HOUR, 1, 2))
    events = engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    kinds = [type(e).__name__ for e in events]
    assert "MilestoneReached" in kinds and "EventCompleted" in kinds
    completed = next(e for e in events if isinstance(e, EventCompleted))
    assert engine.status is EventStatus.COMPLETED
    assert completed.completed_ts == T0 + TOTAL_SECONDS
    assert {p.user_id for p in completed.top3} <= {1, 2}


def test_timer_never_exceeds_160_hours(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + TOTAL_SECONDS + 10 * HOUR, 1))
    assert engine.elapsed(T0 + 500 * HOUR) == pytest.approx(TOTAL_SECONDS)
    assert engine.status is EventStatus.COMPLETED


def test_completion_wins_over_an_empty_vc_at_the_deadline(engine):
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + TOTAL_SECONDS))
    assert any(isinstance(e, EventCompleted) for e in events)
    assert not any(isinstance(e, EventFailed) for e in events)
    assert engine.status is EventStatus.COMPLETED


def test_completion_freezes_the_leaderboard(engine, store):
    start(engine, T0, 1)
    for i in range(1, 31):
        engine.tick(obs(T0 + i, 1, 2))
    engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    frozen = engine.leaderboard()
    assert engine.state.final_saved and frozen
    for i in range(1, 20):
        engine.tick(obs(T0 + TOTAL_SECONDS + i, 1, 2))
    assert [(e.user_id, e.seconds) for e in engine.leaderboard()] == [
        (e.user_id, e.seconds) for e in frozen
    ]


# ------------------------------------------------------------------ misc

def test_cancel_marks_cancelled_not_failed(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 5, 1))
    event = engine.cancel(now=T0 + 6, by_user_id=42)
    assert engine.status is EventStatus.CANCELLED
    assert event.by_user_id == 42
    assert engine.state.final_saved


def test_reset_clears_everything(engine, store):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 32 * HOUR, 1))
    engine.reset()
    assert engine.status is EventStatus.IDLE
    assert engine.event_uid is None
    assert engine.leaderboard() == []


def test_restart_recovers_timer_and_times(engine, store, config):
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    reborn = HellEngine(store, config)
    assert reborn.status is EventStatus.RUNNING
    assert reborn.state.start_ts == T0
    assert reborn.elapsed(T0 + 120) == 120  # timestamp based, not uptime based
    assert reborn.leaderboard()[0].seconds == pytest.approx(60, abs=1.5)


def test_rapid_join_leave_churn_is_tracked(engine):
    start(engine, T0, 1)
    t = T0
    for i in range(60):
        t += 1
        engine.tick(obs(t, 1, 2) if i % 2 == 0 else obs(t, 1))
    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert engine.status is EventStatus.RUNNING
    assert board[1] == pytest.approx(60, abs=2)
    assert board[2] == pytest.approx(30, abs=2)


# --------------------------------------------- downtime does not lose progress

def test_a_short_outage_is_credited_back_to_whoever_never_left(engine, store, config):
    """The bot dying must not cost people the time they were actually there."""
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    before = {e.user_id: e.seconds for e in engine.leaderboard()}

    reborn = HellEngine(store, config)                 # restart after 3 minutes down
    reborn.tick(obs(T0 + 60 + 180, 1))                 # user 1 still there, user 2 left

    after = {e.user_id: e.seconds for e in reborn.leaderboard()}
    assert after[1] == pytest.approx(before[1] + 180, abs=1)   # made whole
    assert after[2] == pytest.approx(before[2], abs=1)         # nothing invented


def test_a_long_outage_is_not_credited(engine, store, config):
    """Beyond the allowance the bot cannot claim to know what happened."""
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    before = engine.leaderboard()[0].seconds

    reborn = HellEngine(store, config)
    reborn.tick(obs(T0 + 60 + 3600, 1))                # an hour of downtime

    assert reborn.leaderboard()[0].seconds <= before + config.max_tick_credit + 1
    assert store.get_unverified_seconds(reborn.event_uid) > 3000


def test_nobody_who_arrived_during_the_outage_gets_free_time(engine, store, config):
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))

    reborn = HellEngine(store, config)
    reborn.tick(obs(T0 + 60 + 120, 1, 7))              # user 7 appears after the gap

    board = {e.user_id: e.seconds for e in reborn.leaderboard()}
    assert board[1] > 150                               # was there throughout
    assert board[7] <= config.max_tick_credit + 1       # only what was observed


def test_the_allowance_is_configurable(tmp_path):
    from hell.storage import Store

    config = make_config(tmp_path, downtime_credit_seconds=30.0)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    engine.tick(obs(T0 + 1, 1))
    engine.tick(obs(T0 + 61, 1))                        # a 60s gap, over the 30s allowance
    assert engine.leaderboard()[0].seconds < 20
    store.close()


# --------------------------------------------------------------- pause/resume

def test_pause_freezes_the_global_timer(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))
    engine.pause(now=T0 + 60)
    assert engine.is_paused
    # Ticks during the pause are inert — even a day of wall time changes nothing.
    engine.tick(obs(T0 + 60 + 86_400, 1))
    assert engine.elapsed(T0 + 60 + 86_400) == pytest.approx(60.0)
    engine.resume(now=T0 + 60 + 86_400)
    assert not engine.is_paused
    assert engine.elapsed(T0 + 60 + 86_400) == pytest.approx(60.0)
    engine.tick(obs(T0 + 60 + 86_400 + 1, 1))
    assert engine.elapsed(T0 + 60 + 86_400 + 1) == pytest.approx(61.0)


def test_pause_freezes_per_user_clocks(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))
    engine.pause(now=T0 + 60)
    for t in range(61, 3600):
        engine.tick(obs(T0 + t, 1))               # an hour of paused ticks
    engine.resume(now=T0 + 3600)
    engine.tick(obs(T0 + 3601, 1))
    seconds = engine.leaderboard()[0].seconds
    assert seconds == pytest.approx(61.0)         # 60 before + 1 after, none during


def test_pause_prevents_milestones_and_completion(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 31 * 3600, 1))
    engine.pause(now=T0 + 31 * 3600)
    engine.tick(obs(T0 + 33 * 3600, 1))           # effective 31h — no milestone yet
    assert engine.milestone_records() == []
    assert engine.status is EventStatus.RUNNING
    engine.resume(now=T0 + 33 * 3600)
    engine.tick(obs(T0 + 34 * 3600, 1))           # effective 32h — milestone fires
    assert [r.hours for r in engine.milestone_records()] == [32]


def test_grace_window_is_frozen_during_a_pause(engine):
    start(engine, T0, 1)
    opened = engine.tick(obs(T0 + 10))            # VC empties -> 15s grace opens
    assert isinstance(opened[0], GraceStarted)
    engine.pause(now=T0 + 10)
    # Wall time sails far past the original deadline while paused...
    engine.tick(obs(T0 + 10 + 9_999))
    assert engine.status is EventStatus.RUNNING   # ...and nothing fails
    engine.resume(now=T0 + 10 + 10_000)           # deadline shifts forward by the pause
    engine.tick(obs(T0 + 10 + 10_001))            # still inside the shifted window
    assert engine.status is EventStatus.RUNNING
    engine.tick(obs(T0 + 10 + 10_016))            # window finally expires
    assert engine.status is EventStatus.FAILED


def test_pause_is_refused_unless_running(engine):
    with pytest.raises(StartError):
        engine.pause(now=T0)


def test_double_pause_is_refused(engine):
    start(engine, T0, 1)
    engine.pause(now=T0 + 1)
    with pytest.raises(StartError):
        engine.pause(now=T0 + 2)
    # ...but a pause then resume works
    engine.resume(now=T0 + 4)
    assert not engine.is_paused


def test_resume_when_not_paused_is_refused(engine):
    start(engine, T0, 1)
    with pytest.raises(StartError):
        engine.resume(now=T0 + 1)


def test_pause_survives_a_restart(tmp_path):
    from hell.storage import Store

    config = make_config(tmp_path)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))
    engine.pause(now=T0 + 60, reason="bug fixing")
    store.close()

    # Restart while still paused: the event must come back frozen.
    store2 = Store(config.database_path)
    engine2 = HellEngine(store2, config)
    assert engine2.is_paused
    assert engine2.state.pause_reason == "bug fixing"
    assert engine2.elapsed(T0 + 7200) == pytest.approx(60.0)
    engine2.tick(obs(T0 + 7200, 1))               # inert while paused
    assert engine2.elapsed(T0 + 7200) == pytest.approx(60.0)
    engine2.resume(now=T0 + 7200)
    assert engine2.state.paused_seconds == pytest.approx(7140.0)
    engine2.tick(obs(T0 + 7201, 1))
    assert engine2.elapsed(T0 + 7201) == pytest.approx(61.0)
    store2.close()


def test_completion_after_a_pause_counts_exactly_160h(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 159 * HOUR, 1))
    engine.pause(now=T0 + 159 * HOUR)
    engine.resume(now=T0 + 159 * HOUR + 3 * HOUR)     # a 3h pause
    engine.tick(obs(T0 + 159 * HOUR + 3 * HOUR + 3601, 1))  # effective 160h 1s
    assert engine.status is EventStatus.COMPLETED
    assert engine.elapsed() == pytest.approx(TOTAL_SECONDS)  # clamped, not 163h


def test_resume_failed_run(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 100, 1))
    assert engine.elapsed(T0 + 100) == pytest.approx(100.0)

    # Empty VC -> fails after grace
    engine.tick(obs(T0 + 101))
    engine.tick(obs(T0 + 101 + GRACE))
    assert engine.status is EventStatus.FAILED
    assert engine.elapsed(T0 + 500) == pytest.approx(101.0)

    # Resume the failed run at T0 + 500
    engine.resume_failed(now=T0 + 500)
    assert engine.status is EventStatus.RUNNING
    assert engine.elapsed(T0 + 500) == pytest.approx(101.0)

    # Further ticks continue the clock
    engine.tick(obs(T0 + 501, 1))
    assert engine.elapsed(T0 + 501) == pytest.approx(102.0)
    board = {e.user_id: e.seconds for e in engine.leaderboard()}
    assert board[1] == pytest.approx(101.0)


def test_resume_failed_shifts_next_alive_check(engine):
    from hell.alivecheck import AliveCheckManager

    class _IO:
        async def send_check(self, *a, **k):
            return None

        async def send_result(self, *a, **k):
            return None

        async def kick(self, *a, **k):
            return []

        async def mute(self, *a, **k):
            return False

        async def replies_since(self, *a, **k):
            return set()

    start(engine, T0, 1)
    checks = AliveCheckManager(engine.config, engine.store, _IO(), engine=engine)
    checks.bind(engine.event_uid, now=T0)
    engine._alive_checks = checks
    due = T0 + 3600
    engine.store.set_next_alive_check(engine.event_uid, due)

    engine.tick(obs(T0 + 100, 1))
    engine.tick(obs(T0 + 101))
    engine.tick(obs(T0 + 101 + GRACE))
    engine.resume_failed(now=T0 + 500)

    shifted = checks.next_check_ts()
    assert shifted is not None
    assert shifted == pytest.approx(due + engine.state.paused_seconds, abs=1.5)
    assert shifted > T0 + 500


def test_resume_failed_when_not_failed_is_refused(engine):
    start(engine, T0, 1)
    with pytest.raises(StartError, match="not failed"):
        engine.resume_failed(now=T0 + 10)


# ------------------------------------------------------- Hell 2 continuation


def _complete_160h(engine):
    start(engine, T0, 1, 2)
    events = engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    assert engine.status is EventStatus.COMPLETED
    return events


def test_resume_continuation_extends_to_320h_without_milestones(engine, store):
    _complete_160h(engine)
    store.record_continuation_vote(engine.event_uid, 1, "yes", T0)
    store.record_continuation_vote(engine.event_uid, 2, "yes", T0)

    # Wait 2 hours before the host resumes; that time is banked, not counted.
    resume_at = T0 + TOTAL_SECONDS + 2 * HOUR
    engine.resume_continuation(now=resume_at)
    assert engine.status is EventStatus.RUNNING
    assert engine.is_continuation
    assert engine.state.total_seconds == 320 * HOUR
    assert not engine.state.milestones_enabled
    assert engine.elapsed(resume_at) == pytest.approx(TOTAL_SECONDS)

    # Past the old 160h point there are no milestone events.
    tick_at = resume_at + 100
    events = engine.tick(obs(tick_at, 1))
    assert not [e for e in events if isinstance(e, MilestoneReached)]
    assert engine.status is EventStatus.RUNNING
    assert engine.elapsed(tick_at) == pytest.approx(TOTAL_SECONDS + 100)

    # Completion happens at 320h of effective event time.
    finish_at = resume_at + (320 * HOUR - TOTAL_SECONDS)
    events = engine.tick(obs(finish_at, 1))
    completed = next(e for e in events if isinstance(e, EventCompleted))
    assert engine.status is EventStatus.COMPLETED
    assert completed.continuation is True
    assert engine.state.total_seconds == 320 * HOUR


def test_resume_continuation_requires_yes_majority(engine, store):
    _complete_160h(engine)
    store.record_continuation_vote(engine.event_uid, 1, "no", T0)
    store.record_continuation_vote(engine.event_uid, 2, "no", T0)
    with pytest.raises(StartError, match="vote has not passed"):
        engine.resume_continuation(now=T0 + TOTAL_SECONDS + 1, approved=False)


def test_resume_continuation_not_allowed_on_a_failed_run(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 1))
    engine.tick(obs(T0 + 1 + GRACE))
    with pytest.raises(StartError, match="not completed"):
        engine.resume_continuation(now=T0 + 1000)


def test_alive_check_recovery_grace_duration(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))

    # Simulate alive check emptying the VC
    engine.notify_alive_check_emptied(T0 + 61)
    opened = engine.tick(obs(T0 + 61))
    assert opened and opened[0].seconds == 120.0
    assert engine.grace.seconds == 120.0

    # 15s in -> still running (not expired)
    engine.tick(obs(T0 + 61 + 15))
    assert engine.status is EventStatus.RUNNING

    # 60s in -> still running
    engine.tick(obs(T0 + 61 + 60))
    assert engine.status is EventStatus.RUNNING

    # 120s in -> expires and fails
    expired = engine.tick(obs(T0 + 61 + 120))
    assert engine.status is EventStatus.FAILED
    assert expired and type(expired[0]).__name__ == "EventFailed"


def test_alive_check_recovery_grace_survives_restart(tmp_path):
    from hell.storage import Store

    config = make_config(tmp_path)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60, 1))
    engine.notify_alive_check_emptied(T0 + 61)
    engine.tick(obs(T0 + 61))
    assert engine.grace.seconds == 120.0
    store.close()

    # Restart mid-recovery-grace
    store2 = Store(config.database_path)
    engine2 = HellEngine(store2, config)
    assert engine2.grace.is_open
    assert engine2.grace.seconds == 120.0
    assert engine2.grace.deadline() == T0 + 61 + 120.0

    # Tick before 120s -> still running
    engine2.tick(obs(T0 + 61 + 50))
    assert engine2.status is EventStatus.RUNNING

    # Recover by joining
    recovered = engine2.tick(obs(T0 + 61 + 70, 1))
    assert engine2.status is EventStatus.RUNNING
    assert recovered and type(recovered[0]).__name__ == "GraceRecovered"
    assert engine2.grace.seconds == config.empty_vc_grace_seconds
    store2.close()
