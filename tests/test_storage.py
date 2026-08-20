"""Persistence guarantees."""

from __future__ import annotations

from hell.models import EventStatus, LeaderboardEntry, ParticipantRef
from hell.storage import Store
from tests.conftest import T0, obs, start


def test_state_round_trip(config):
    store = Store(config.database_path)
    state = store.load_state()
    assert state.status is EventStatus.IDLE
    state.status = EventStatus.RUNNING
    state.event_uid = "abc"
    state.start_ts = T0
    store.save_state(state)
    store.close()

    reopened = Store(config.database_path)
    loaded = reopened.load_state()
    assert loaded.status is EventStatus.RUNNING
    assert loaded.event_uid == "abc"
    assert loaded.start_ts == T0
    reopened.close()


def test_claim_milestone_is_atomic(store):
    assert store.claim_milestone("uid", 32, T0) is True
    assert store.claim_milestone("uid", 32, T0) is False
    assert store.triggered_milestone_hours("uid") == {32}


def test_pending_announcements_survive_a_crash(store):
    store.claim_milestone("uid", 32, T0)
    store.save_milestone_members("uid", 32, [ParticipantRef(1, "User1")])
    pending = store.pending_announcements("uid")
    assert [p.hours for p in pending] == [32]
    store.mark_milestone_announced("uid", 32)
    assert store.pending_announcements("uid") == []
    assert store.get_milestones("uid")[0].members[0].user_id == 1


def test_user_time_accumulates_across_calls(store):
    store.add_user_time("uid", [(1, "User1", 10.0, T0)])
    store.add_user_time("uid", [(1, "User1 renamed", 5.0, T0 + 5)])
    rows = store.get_user_times("uid")
    assert rows == [(1, "User1 renamed", 15.0)]


def test_final_leaderboard_is_frozen(store):
    store.save_final_leaderboard("uid", [LeaderboardEntry(1, 7, "Seven", 100.0)])
    saved = store.get_final_leaderboard("uid")
    assert saved[0].user_id == 7 and saved[0].seconds == 100.0
    assert store.load_state().final_saved is True


def test_presence_is_replaced_not_appended(store):
    store.replace_presence("uid", [ParticipantRef(1, "a"), ParticipantRef(2, "b")], T0)
    store.replace_presence("uid", [ParticipantRef(2, "b")], T0 + 1)
    assert [p.user_id for p in store.get_presence("uid")] == [2]


def test_reset_all_clears_the_database(store, engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 1, 1))
    uid = engine.event_uid
    store.reset_all()
    assert store.load_state().status is EventStatus.IDLE
    assert store.get_user_times(uid) == []
    assert store.get_presence(uid) == []


def test_presence_is_only_rewritten_when_it_changes(engine, store, monkeypatch):
    """Over a 160h run this saves ~576k pointless writes."""
    calls = {"n": 0}
    original = store.replace_presence

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "replace_presence", counting)
    start(engine, T0, 1, 2)
    baseline = calls["n"]                        # the initial snapshot at /hell start
    for i in range(1, 31):
        engine.tick(obs(T0 + i, 1, 2))          # stable membership -> no writes
    assert calls["n"] == baseline
    engine.tick(obs(T0 + 31, 1))                 # someone leaves -> one write
    assert calls["n"] == baseline + 1
