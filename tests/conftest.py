"""Shared test fixtures — the engine runs entirely without Discord."""

from __future__ import annotations

from pathlib import Path

import pytest

from hell.config import Config
from hell.engine import HellEngine, Observation
from hell.models import ParticipantRef
from hell.storage import Store

HOUR = 3600.0
T0 = 1_700_000_000.0  # fixed absolute start timestamp for deterministic tests


@pytest.fixture(autouse=True)
def isolate_status_file(tmp_path_factory):
    """Prevent tests from writing to the repo's tracked docs/status.json."""
    temp_docs = tmp_path_factory.mktemp("docs_status")
    from hell.status_writer import StatusFile

    orig_write = StatusFile.write

    def isolated_write(self, path="docs/status.json", *args, **kwargs):
        if str(path) == "docs/status.json" or Path(path) == Path("docs/status.json"):
            path = temp_docs / "status.json"
        return orig_write(self, path, *args, **kwargs)

    StatusFile.write = isolated_write
    yield
    StatusFile.write = orig_write


@pytest.fixture(autouse=True)
def isolate_live_server_file(tmp_path_factory):
    """Prevent tests from writing to the repo's tracked docs/live-server.json.

    Manual save/restore on purpose: depending on ``monkeypatch`` here would
    move its teardown and break fixtures that rely on their own monkeypatches
    being undone in a specific order.
    """
    temp_docs = tmp_path_factory.mktemp("docs_live_server")

    from hell import monitor as monitor_mod
    from hell import pages_sync

    orig = pages_sync.write_live_server_file

    def isolated(repo_root=".", url="", *args, **kwargs):
        return orig(temp_docs, url, *args, **kwargs)

    pages_sync.write_live_server_file = isolated
    monitor_mod.write_live_server_file = isolated  # imported by reference
    yield temp_docs
    pages_sync.write_live_server_file = orig
    monitor_mod.write_live_server_file = orig


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


GRACE = 15.0  # default EMPTY_VC_GRACE_SECONDS


def empty_out(engine: HellEngine, at: float, *, grace: float = GRACE):
    """Empty the VC and let the grace window expire -> the run fails.

    Returns (grace_events, failure_events).
    """
    opened = engine.tick(obs(at))
    expired = engine.tick(obs(at + grace))
    return opened, expired


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
