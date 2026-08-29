"""Single-instance guard: only one live bot process per database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hell.single_instance import LOCK_NAME, InstanceLock, SingleInstanceError


def test_acquire_then_second_acquire_raises(tmp_path: Path) -> None:
    db = tmp_path / "hell.sqlite3"
    first = InstanceLock(db)
    first.acquire()
    try:
        assert first.held
        assert (tmp_path / LOCK_NAME).exists()
        second = InstanceLock(db)
        with pytest.raises(SingleInstanceError) as excinfo:
            second.acquire()
        assert str(first.path) in str(excinfo.value)
        assert not second.held
    finally:
        first.release()


def test_release_allows_a_new_instance(tmp_path: Path) -> None:
    db = tmp_path / "hell.sqlite3"
    first = InstanceLock(db)
    first.acquire()
    first.release()
    assert not first.held
    # Releasing twice is a no-op.
    first.release()
    second = InstanceLock(db)
    second.acquire()
    assert second.held
    second.release()


def test_acquiring_twice_on_the_same_lock_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "x.sqlite3")
    lock.acquire()
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held


def test_different_databases_can_run_in_parallel(tmp_path: Path) -> None:
    a = InstanceLock(tmp_path / "a" / "hell.sqlite3")
    b = InstanceLock(tmp_path / "b" / "hell.sqlite3")
    a.acquire()
    b.acquire()  # a different directory is a different deployment
    assert a.held and b.held
    a.release()
    b.release()


def test_lock_file_records_the_pid(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "hell.sqlite3")
    lock.acquire()
    try:
        assert lock.path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_context_manager_releases(tmp_path: Path) -> None:
    db = tmp_path / "hell.sqlite3"
    with InstanceLock(db) as lock:
        assert lock.held
    assert not lock.held
    # The slot is free again after the block ends.
    InstanceLock(db).acquire()


def test_second_instance_error_carries_the_lock_path(tmp_path: Path) -> None:
    db = tmp_path / "hell.sqlite3"
    first = InstanceLock(db)
    first.acquire()
    try:
        second = InstanceLock(db)
        with pytest.raises(SingleInstanceError) as excinfo:
            second.acquire()
        assert excinfo.value.lock_path == db.parent / LOCK_NAME
    finally:
        first.release()


def test_hellbot_refuses_a_second_instance_with_the_same_database(config) -> None:
    """The guard lives in HellBot.__init__, so every entry point (console,
    Docker CMD, GUI launcher) is covered by it."""
    from hell.bot import build_bot

    first = build_bot(config)
    try:
        assert first.instance_lock.held
        with pytest.raises(SingleInstanceError):
            build_bot(config)
    finally:
        first.store.close()
