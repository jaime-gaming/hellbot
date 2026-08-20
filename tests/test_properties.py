"""Property/chaos tests: thousands of random ticks, invariants must hold.

Unit tests check known scenarios; this file throws randomised sequences of
joins, leaves, empty channels, restarts and clock jumps at the engine and
asserts the rules that must *never* break, whatever happens.
"""

from __future__ import annotations

import random

import pytest

from hell.engine import (
    EventCompleted,
    EventFailed,
    GraceRecovered,
    GraceStarted,
    HellEngine,
    MilestoneReached,
    Observation,
)
from hell.milestones import TOTAL_SECONDS
from hell.models import EventStatus, ParticipantRef
from tests.conftest import GRACE, T0, make_config

SEEDS = [1, 7, 13, 42, 99, 2024, 31337]


def people(rng: random.Random, pool: int = 6) -> tuple[ParticipantRef, ...]:
    count = rng.choice([0, 0, 1, 1, 2, 3, 4, pool])
    return tuple(ParticipantRef(i, f"User{i}") for i in rng.sample(range(1, pool + 1), count))


def fresh(tmp_path, name: str, **overrides):
    from hell.storage import Store

    config = make_config(tmp_path, database_path=tmp_path / f"{name}.sqlite3", **overrides)
    store = Store(config.database_path)
    return HellEngine(store, config), store, config


def begin(engine: HellEngine, now: float, ids=(1,)):
    return engine.start(
        now=now,
        guild_id=1,
        voice_channel_id=1,
        announce_channel_id=2,
        started_by=99,
        initial_participants=[ParticipantRef(i, f"User{i}") for i in ids],
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_invariants_hold_under_random_traffic(tmp_path, seed):
    """The core promises, checked after every single tick."""
    rng = random.Random(seed)
    engine, store, _config = fresh(tmp_path, f"chaos{seed}")
    begin(engine, T0, (1, 2))

    now = T0
    seen_milestones: list[int] = []
    terminal_at: float | None = None
    last_totals: dict[int, float] = {}
    empty_since: float | None = None

    for _ in range(1500):
        now += rng.choice([1, 1, 1, 2, 5, 30, 300, 3600])
        crowd = people(rng)
        events = engine.tick(Observation(now=now, participants=crowd))
        elapsed = engine.elapsed(now)

        # 1. the clock is bounded and never rewinds
        assert 0 <= elapsed <= TOTAL_SECONDS
        if terminal_at is not None:
            assert engine.elapsed(now) == pytest.approx(engine.elapsed(now + 10_000))

        # 2. a terminal event stays terminal and emits nothing further
        if terminal_at is not None:
            assert engine.status.is_terminal
            assert events == []

        # 3. per-user totals never decrease and never exceed the event clock
        totals = {e.user_id: e.seconds for e in engine.leaderboard()}
        for uid, seconds in totals.items():
            assert seconds >= last_totals.get(uid, 0.0) - 1e-6
            assert seconds <= elapsed + 1.0
        last_totals = totals

        # 4. milestones fire once, in order, only while people are present
        for event in events:
            if isinstance(event, MilestoneReached):
                assert event.milestone.hours not in seen_milestones
                assert not seen_milestones or event.milestone.hours > seen_milestones[-1]
                seen_milestones.append(event.milestone.hours)
                assert event.members, "a milestone must have someone able to claim it"
            if isinstance(event, (EventFailed, EventCompleted)):
                terminal_at = now

        # 5. failure only ever follows a grace window that really expired
        if any(isinstance(e, EventFailed) for e in events):
            assert empty_since is not None
            assert now - empty_since >= GRACE - 1e-6
        if crowd:
            empty_since = None
        elif empty_since is None:
            empty_since = now

    assert engine.status in (EventStatus.RUNNING, EventStatus.FAILED, EventStatus.COMPLETED)
    store.close()


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_state_survives_random_restarts(tmp_path, seed):
    """Rebuilding the engine from disk mid-run must change nothing observable."""
    rng = random.Random(seed)
    engine, store, config = fresh(tmp_path, f"restart{seed}")
    begin(engine, T0, (1, 2, 3))

    now = T0
    for _ in range(400):
        now += rng.choice([1, 1, 2, 60])
        crowd = tuple(ParticipantRef(i, f"User{i}") for i in rng.sample([1, 2, 3], rng.choice([1, 2, 3])))
        engine.tick(Observation(now=now, participants=crowd))

        if rng.random() < 0.05:  # simulate a restart
            before = (
                engine.status,
                round(engine.elapsed(now), 3),
                {e.user_id: round(e.seconds, 3) for e in engine.leaderboard()},
                engine.store.triggered_milestone_hours(engine.event_uid),
                engine.grace.empty_since,
            )
            engine = HellEngine(store, config)
            after = (
                engine.status,
                round(engine.elapsed(now), 3),
                {e.user_id: round(e.seconds, 3) for e in engine.leaderboard()},
                engine.store.triggered_milestone_hours(engine.event_uid),
                engine.grace.empty_since,
            )
            assert before == after
    store.close()


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_grace_window_never_loses_or_invents_a_failure(tmp_path, seed):
    """Empty ➜ warning; back in time ➜ recovery; timed out ➜ exactly one failure."""
    rng = random.Random(seed)
    engine, store, _config = fresh(tmp_path, f"grace{seed}")
    begin(engine, T0, (1,))

    now = T0
    open_window = False
    failures = 0
    for _ in range(600):
        now += rng.choice([1, 2, 5, 14, 16])
        crowd = () if rng.random() < 0.35 else (ParticipantRef(1, "User1"),)
        events = engine.tick(Observation(now=now, participants=crowd))

        for event in events:
            if isinstance(event, GraceStarted):
                assert not open_window, "two warnings for one empty period"
                open_window = True
            elif isinstance(event, GraceRecovered):
                assert open_window, "recovery without a warning"
                open_window = False
            elif isinstance(event, EventFailed):
                failures += 1
                open_window = False
        if engine.status.is_terminal:
            break

    assert failures <= 1
    if engine.status is EventStatus.FAILED:
        assert failures == 1
    store.close()


def test_no_time_is_credited_while_the_vc_is_empty(tmp_path):
    engine, store, _config = fresh(tmp_path, "credit")
    begin(engine, T0, (1,))
    rng = random.Random(5)

    now = T0
    empty_seconds = 0.0
    for _ in range(300):
        step = rng.choice([1, 2, 3])
        now += step
        crowd = () if rng.random() < 0.25 else (ParticipantRef(1, "User1"),)
        if not crowd:
            empty_seconds += step
        engine.tick(Observation(now=now, participants=crowd))
        if engine.status.is_terminal:
            break

    total = engine.leaderboard()[0].seconds if engine.leaderboard() else 0.0
    assert total <= engine.elapsed(now) - empty_seconds + 5
    store.close()


def test_a_marathon_run_completes_exactly_once(tmp_path):
    """160 hours end to end: one completion, five milestones, no drift.

    Sampled every 60s with a matching credit cap — the live bot samples every
    second with a 5s cap, which is the same relationship at 60x the speed.
    """
    engine, store, _config = fresh(tmp_path, "marathon", max_tick_credit=61.0)
    begin(engine, T0, (1, 2))
    crowd = (ParticipantRef(1, "User1"), ParticipantRef(2, "User2"))

    completions = 0
    milestones: list[int] = []
    now = T0
    while now < T0 + TOTAL_SECONDS + 600:
        now += 60
        for event in engine.tick(Observation(now=now, participants=crowd)):
            if isinstance(event, EventCompleted):
                completions += 1
            elif isinstance(event, MilestoneReached):
                milestones.append(event.milestone.hours)

    assert completions == 1
    assert milestones == [32, 64, 96, 128, 160]
    assert engine.status is EventStatus.COMPLETED
    assert engine.elapsed(now + 10_000) == pytest.approx(TOTAL_SECONDS)
    board = engine.leaderboard()
    assert all(e.seconds == pytest.approx(TOTAL_SECONDS, abs=120) for e in board)
    store.close()
