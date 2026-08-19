"""Shared test fixtures — the engine runs entirely without Discord."""

from __future__ import annotations

import pytest

from hell.config import Config
from hell.engine import HellEngine, Observation
from hell.models import ParticipantRef
from hell.storage import Store

HOUR = 3600.0
T0 = 1_700_000_000.0  # fixed absolute start timestamp for deterministic tests


def make_config(tmp_path, **overrides) -> Config:
    kwargs = dict(
        token="test",
        guild_id=1,
        voice_channel_id=1539756705997652079,
        announce_channel_id=2,
        gamenight_host_role_id=3,
        clanker_role_id=4,
        database_path=tmp_path / "hell.sqlite3",
        max_tick_credit=5.0,
        monitor_interval=1.0,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def store(config):
    s = Store(config.database_path)
    yield s
    s.close()


@pytest.fixture
def engine(store, config):
    return HellEngine(store, config)


def users(*ids: int) -> tuple[ParticipantRef, ...]:
    return tuple(ParticipantRef(i, f"User{i}") for i in ids)


def obs(now: float, *ids: int) -> Observation:
    return Observation(now=now, participants=users(*ids))


def start(engine: HellEngine, now: float = T0, *ids: int):
    return engine.start(
        now=now,
        guild_id=1,
        voice_channel_id=1539756705997652079,
        announce_channel_id=2,
        started_by=99,
        initial_participants=users(*ids),
    )


def run_seconds(engine: HellEngine, *, start_at: float, seconds: int, ids=(1,), step: float = 1.0):
    """Feed `seconds` worth of 1-second observations, returning all events."""
    events = []
    t = start_at
    end = start_at + seconds
    while t < end:
        t += step
        events.extend(engine.tick(obs(t, *ids)))
    return events
