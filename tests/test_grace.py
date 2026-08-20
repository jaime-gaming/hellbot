"""Empty-VC grace period: warn, recover, or die 15 seconds later."""

from __future__ import annotations

import pytest

from hell.engine import EventFailed, GraceRecovered, GraceStarted, HellEngine
from hell.grace import EmptyVcGracePeriod
from hell.models import EventStatus
from hell.timeline import EventTimeline
from tests.conftest import GRACE, HOUR, T0, empty_out, make_config, obs, start

# --------------------------------------------------------- the pure window

def test_window_opens_and_expires():
    window = EmptyVcGracePeriod(seconds=15)
    assert not window.is_open
    window.open(100.0)
    assert window.is_open and window.deadline() == 115.0
    assert not window.has_expired(114.9)
    assert window.has_expired(115.0)
    assert window.seconds_left(110.0) == 5.0


def test_window_closes_on_recovery():
    window = EmptyVcGracePeriod(seconds=15)
    window.open(100.0)
    assert window.close() == 100.0
    assert not window.is_open
    assert window.close() is None


def test_reopening_keeps_the_original_start():
    window = EmptyVcGracePeriod(seconds=15)
    window.open(100.0)
    window.open(105.0)
    assert window.empty_since == 100.0     # the clock does not restart


# ------------------------------------------------------------ engine rules

def test_empty_vc_warns_first_and_does_not_fail(engine):
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + 60))              # last human leaves
    assert len(events) == 1
    warning = events[0]
    assert isinstance(warning, GraceStarted)
    assert warning.seconds == GRACE
    assert warning.deadline_ts == T0 + 60 + GRACE
    assert engine.status is EventStatus.RUNNING     # still alive!


def test_no_repeated_warning_while_the_window_is_open(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    for i in range(1, int(GRACE)):
        assert engine.tick(obs(T0 + 60 + i)) == []  # silent countdown
    assert engine.status is EventStatus.RUNNING


def test_someone_joining_in_time_saves_the_run(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    events = engine.tick(obs(T0 + 70, 7))           # 10s later, a rescuer
    assert len(events) == 1
    recovered = events[0]
    assert isinstance(recovered, GraceRecovered)
    assert recovered.empty_for == pytest.approx(10.0)
    assert [p.user_id for p in recovered.participants] == [7]
    assert engine.status is EventStatus.RUNNING
    assert not engine.grace.is_open


def test_the_run_survives_repeated_close_calls(engine):
    start(engine, T0, 1)
    t = T0
    for _ in range(5):
        t += 60
        engine.tick(obs(t))                          # empty
        t += GRACE - 1
        engine.tick(obs(t, 1))                       # rescued with 1s to spare
    assert engine.status is EventStatus.RUNNING


def test_the_run_dies_when_nobody_comes_back(engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    events = engine.tick(obs(T0 + 60 + GRACE))
    assert isinstance(events[0], EventFailed)
    assert engine.status is EventStatus.FAILED


def test_failure_is_timestamped_at_the_moment_the_vc_emptied(engine):
    """The grace window must never inflate the survived time."""
    start(engine, T0, 1)
    engine.tick(obs(T0 + 3600, 1))
    engine.tick(obs(T0 + 3600))                      # empty at exactly 1h
    events = engine.tick(obs(T0 + 3600 + GRACE))
    failure = events[0]
    assert failure.failed_ts == T0 + 3600
    assert failure.elapsed == pytest.approx(3600.0)
    assert engine.elapsed(T0 + 10 * HOUR) == pytest.approx(3600.0)


def test_nobody_earns_time_during_the_grace_window(engine):
    start(engine, T0, 1)
    for i in range(1, 11):
        engine.tick(obs(T0 + i, 1))
    before = engine.leaderboard()[0].seconds
    engine.tick(obs(T0 + 11))                        # empty
    engine.tick(obs(T0 + 11 + GRACE - 1))            # still empty
    assert engine.leaderboard()[0].seconds == pytest.approx(before)


def test_the_global_timeline_ignores_individual_users(engine):
    """One user leaving cannot dent the 0 -> 160h clock while others remain."""
    start(engine, T0, 1, 2, 3)
    engine.tick(obs(T0 + 10, 1, 2, 3))
    engine.tick(obs(T0 + 20, 2, 3))
    engine.tick(obs(T0 + 30, 3))
    assert engine.status is EventStatus.RUNNING
    assert engine.elapsed(T0 + 30) == pytest.approx(30.0)
    assert not engine.grace.is_open


def test_zero_length_grace_fails_instantly(tmp_path):
    from hell.storage import Store

    config = make_config(tmp_path, empty_vc_grace_seconds=0.0)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    events = engine.tick(obs(T0 + 5))
    assert isinstance(events[0], EventFailed)
    store.close()


def test_longer_grace_is_configurable(tmp_path):
    from hell.storage import Store

    config = make_config(tmp_path, empty_vc_grace_seconds=60.0)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    start(engine, T0, 1)
    engine.tick(obs(T0 + 10))
    assert engine.tick(obs(T0 + 10 + 30)) == []       # still inside the window
    assert engine.status is EventStatus.RUNNING
    assert isinstance(engine.tick(obs(T0 + 10 + 60))[0], EventFailed)
    store.close()


# --------------------------------------------------------------- restarts

def test_open_window_survives_a_restart(engine, store, config):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))                        # window opens
    assert store.load_state().grace_started_ts == T0 + 60

    reborn = HellEngine(store, config)
    assert reborn.grace.is_open
    assert reborn.grace.empty_since == T0 + 60
    events = reborn.tick(obs(T0 + 60 + GRACE))       # it expires as scheduled
    assert isinstance(events[0], EventFailed)


def test_restart_with_people_back_clears_the_window(engine, store, config):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    reborn = HellEngine(store, config)
    events = reborn.tick(obs(T0 + 65, 1))
    assert isinstance(events[0], GraceRecovered)
    assert reborn.status is EventStatus.RUNNING
    assert store.load_state().grace_started_ts is None


def test_a_new_event_starts_with_a_clean_window(engine, store, config):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    empty_out(engine, T0 + 60 + GRACE)               # let it die
    start(engine, T0 + 10 * HOUR, 1)
    assert not engine.grace.is_open
    assert store.load_state().grace_started_ts is None


# ------------------------------------------------------------- announcing

def test_warning_and_recovery_messages_never_ping(config, engine):
    from hell.announcer import Announcer

    class FakeBot:
        def get_channel(self, _cid):
            return None

    announcer = Announcer(FakeBot(), config, engine)
    start(engine, T0, 1)
    warning = engine.tick(obs(T0 + 60))[0]
    text = announcer.render_grace_warning(warning)
    assert "@everyone" not in text and "@here" not in text
    assert "15 seconds" in text
    assert "No pings on purpose" in text

    recovered = engine.tick(obs(T0 + 65, 1))[0]
    assert "SAVED" in announcer.render_grace_recovered(recovered)


def test_progress_embed_shows_the_countdown(config, engine):
    from hell.announcer import Announcer

    class FakeBot:
        def get_channel(self, _cid):
            return None

    announcer = Announcer(FakeBot(), config, engine)
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    text = announcer.render_progress(engine.snapshot(now=T0 + 65))
    assert "VC EMPTY" in text
    assert "**10s** left" in text


# ---------------------------------------------------------------- timeline

def test_event_timeline_arithmetic():
    timeline = EventTimeline(start_ts=T0)
    assert timeline.deadline == T0 + 160 * HOUR
    assert timeline.elapsed(T0 + 3600) == 3600
    assert timeline.elapsed(T0 + 200 * HOUR) == 160 * HOUR      # never past 160h
    assert timeline.remaining(T0 + 60 * HOUR) == 100 * HOUR
    assert timeline.fraction(T0 + 80 * HOUR) == pytest.approx(0.5)
    assert timeline.is_finished(T0 + 160 * HOUR)
    assert timeline.elapsed(T0 + 99 * HOUR, frozen_at=T0 + 3600) == 3600
    assert timeline.at_hours(32) == T0 + 32 * HOUR


def test_terminating_clears_the_persisted_grace_window(engine, store):
    """Regression: a half-open window must not outlive its run."""
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))                        # window opens, persisted
    assert store.load_state().grace_started_ts is not None
    engine.tick(obs(T0 + 60 + GRACE))                # expires -> FAILED
    assert store.load_state().grace_started_ts is None
    assert not engine.grace.is_open


def test_cancelling_clears_the_grace_window(engine, store):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 60))
    engine.cancel(now=T0 + 65, by_user_id=1)
    assert store.load_state().grace_started_ts is None
