"""Database export — the restorable SQL dump and the human recap."""

from __future__ import annotations

import sqlite3

from hell import dbdump
from tests.conftest import HOUR, obs, start

NOW = 1_750_000_000.0


def _populate(engine):
    """A half-lived event with time, a milestone, wallets and meta rows."""
    start(engine, NOW - 100 * HOUR, 1, 2)
    engine.tick(obs(NOW, 1, 2))
    engine.store.add_user_time(
        engine.event_uid, [(1, "Alice", 3 * HOUR, NOW), (2, "Bob", HOUR, NOW)]
    )
    engine.store.claim_milestone(engine.event_uid, 32, NOW - HOUR)
    engine.add_gamble_seconds(1, "Alice", 2 * HOUR)
    engine.add_gamble_seconds(2, "Bob", -1800.0)
    engine.store.set_meta(f"gamble_cd:{engine.event_uid}:1", str(NOW - 120))
    return engine


def _count(path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_dump_round_trips_the_whole_database(engine):
    _populate(engine)
    path = engine.store.path

    sql = dbdump.dump_database(path).decode("utf-8")
    assert "CREATE TABLE" in sql
    assert sql.count("INSERT INTO") >= 8

    # Restoring the dump into a fresh database reproduces every table exactly.
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    for table in ("event", "user_time", "milestones", "milestone_members",
                  "gamble_time", "meta", "presence"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == _count(path, table)
    assert conn.execute(
        "SELECT display_name FROM user_time WHERE user_id = 1"
    ).fetchone()[0] == "Alice"
    conn.close()


def test_recap_covers_the_key_facts(engine):
    _populate(engine)
    text = dbdump.build_recap(engine.store.path)

    assert "DATABASE RECAP" in text
    assert "RUNNING" in text                       # event status
    assert "Alice" in text and "Bob" in text       # both leaderboards
    assert "32h" in text                           # milestone reached
    assert "wallet" in text                        # gamble section
    assert "gamble_cd:" in text                    # meta keys
    assert "3h 00m" in text                        # human time, not raw seconds
    assert "160h 00m" in text                      # the event total


def test_recap_handles_a_brand_new_empty_database(engine):
    """No event ever started: the recap still renders, without crashing.

    A fresh Store always carries an IDLE event row, so the recap shows the
    idle state with empty sections instead of failing on missing data.
    """
    text = dbdump.build_recap(engine.store.path)
    assert "DATABASE RECAP" in text
    assert "IDLE" in text
    assert "(empty)" in text
    assert "(none)" in text
    assert "(none recorded)" in text


def test_recap_works_while_the_writer_is_alive(engine, monkeypatch):
    """The dump must be taken from a read-only connection (WAL readers
    coexist with the bot's writer) — prove the file is opened with mode=ro."""
    _populate(engine)
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(database, *args, **kwargs):
        opened.append(database if isinstance(database, str) else str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(dbdump.sqlite3, "connect", spy_connect)
    dbdump.build_recap(engine.store.path)
    assert any("mode=ro" in o for o in opened), f"expected a read-only connection, got {opened}"
