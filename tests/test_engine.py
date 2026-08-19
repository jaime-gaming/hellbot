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
from tests.conftest import GRACE, HOUR, T0, empty_out, obs, start


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
    milestone = [e for e in events if isinstance(e, MilestoneReached)][0]
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
    completed = [e for e in events if isinstance(e, EventCompleted)][0]
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
