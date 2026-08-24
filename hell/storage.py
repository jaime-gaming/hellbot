"""Persistence layer — SQLite, single file, crash safe.

Everything the event needs to survive a restart lives here:
status, start timestamp, per-user accumulated time, triggered milestones and
their member snapshots, the frozen final leaderboard and the progress message
reference.

The store is deliberately synchronous: writes are tiny (a few rows per second
at most) and SQLite in WAL mode handles that comfortably, while keeping the
code trivially testable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Optional

from .models import EventState, EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef

log = logging.getLogger("hell.storage")

SCHEMA_VERSION = 4

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    status               TEXT    NOT NULL,
    event_uid            TEXT,
    start_ts             REAL,
    end_ts               REAL,
    last_tick_ts         REAL,
    guild_id             INTEGER,
    voice_channel_id     INTEGER,
    announce_channel_id  INTEGER,
    progress_channel_id  INTEGER,
    progress_message_id  INTEGER,
    started_by           INTEGER,
    end_reason           TEXT,
    final_saved          INTEGER NOT NULL DEFAULT 0,
    grace_started_ts     REAL,             -- empty-VC grace window in progress
    grace_duration       REAL,             -- duration of the active grace window
    last_valid_observed_ts REAL,           -- last tick with at least one valid human [crash-recovery]
    paused_ts            REAL,             -- event paused: global + per-user timers frozen
    paused_seconds       REAL NOT NULL DEFAULT 0,  -- total paused time, never counted
    pause_reason         TEXT,             -- why it was paused (audit trail)
    total_seconds        REAL NOT NULL DEFAULT 576000.0,  -- 160h normally, 320h after continuation
    milestones_enabled   INTEGER NOT NULL DEFAULT 1,     -- phase 2 continuation has no milestones
    continuation         INTEGER NOT NULL DEFAULT 0      -- Hell 2 (320h) continuation run
);

CREATE TABLE IF NOT EXISTS user_time (
    event_uid    TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    seconds      REAL    NOT NULL DEFAULT 0,
    display_name TEXT    NOT NULL DEFAULT '',
    first_seen   REAL,
    last_seen    REAL,
    PRIMARY KEY (event_uid, user_id)
);

CREATE TABLE IF NOT EXISTS milestones (
    event_uid  TEXT    NOT NULL,
    hours      INTEGER NOT NULL,
    reached_ts REAL    NOT NULL,
    announced  INTEGER NOT NULL DEFAULT 0,
    late       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_uid, hours)
);

CREATE TABLE IF NOT EXISTS milestone_members (
    event_uid    TEXT    NOT NULL,
    hours        INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    display_name TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (event_uid, hours, user_id)
);

CREATE TABLE IF NOT EXISTS presence (
    event_uid    TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    display_name TEXT    NOT NULL DEFAULT '',
    since_ts     REAL,
    PRIMARY KEY (event_uid, user_id)
);

CREATE TABLE IF NOT EXISTS alive_check (
    event_uid TEXT PRIMARY KEY,     -- at most one roll call in flight
    payload   TEXT NOT NULL         -- JSON blob (see hell.alivecheck.PendingCheck)
);

CREATE TABLE IF NOT EXISTS alive_check_history (
    event_uid   TEXT    NOT NULL,
    check_id    TEXT    NOT NULL,
    started_ts  REAL    NOT NULL,
    resolved_ts REAL    NOT NULL,
    required    INTEGER NOT NULL,
    responded   INTEGER NOT NULL,
    kicked      TEXT    NOT NULL DEFAULT '[]',
    cancelled   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_uid, check_id)
);

CREATE TABLE IF NOT EXISTS dm_log (
    event_uid TEXT    NOT NULL,
    user_id   INTEGER NOT NULL,
    sent_ts   REAL    NOT NULL,
    status    TEXT    NOT NULL,   -- sent | blocked | failed
    PRIMARY KEY (event_uid, user_id)
);

CREATE TABLE IF NOT EXISTS final_leaderboard (
    event_uid    TEXT    NOT NULL,
    rank         INTEGER NOT NULL,   -- ties share a rank, hence not part of the key
    position     INTEGER NOT NULL,   -- stable display order
    user_id      INTEGER NOT NULL,
    display_name TEXT    NOT NULL DEFAULT '',
    seconds      REAL    NOT NULL,
    PRIMARY KEY (event_uid, user_id)
);
CREATE INDEX IF NOT EXISTS idx_final_order ON final_leaderboard(event_uid, position);

CREATE TABLE IF NOT EXISTS hell_events (
    id             TEXT PRIMARY KEY,
    event_uid      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    name           TEXT NOT NULL,
    start_ts       REAL NOT NULL,
    end_ts         REAL NOT NULL,
    state          TEXT NOT NULL,
    affected_users TEXT NOT NULL DEFAULT '[]',
    metadata       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hell_events_uid ON hell_events(event_uid);

CREATE TABLE IF NOT EXISTS continuation_poll (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    event_uid   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'none',   -- none | open | closed | resumed
    channel_id  INTEGER,
    message_id  INTEGER,
    started_ts  REAL,
    deadline_ts REAL,
    result      TEXT
);

CREATE TABLE IF NOT EXISTS continuation_votes (
    event_uid TEXT NOT NULL,
    user_id   INTEGER NOT NULL,
    answer    TEXT NOT NULL,               -- yes | no
    voted_ts  REAL NOT NULL,
    PRIMARY KEY (event_uid, user_id)
);
"""


class Store:
    """Thread-safe SQLite wrapper holding the whole event state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO event(id, status) VALUES (1, ?)",
                (EventStatus.IDLE.value,),
            )
            self._migrate()
            log.info(
                "Database ready: %s (schema v%d)",
                self.path if str(self.path) != ":memory:" else "in-memory",
                SCHEMA_VERSION,
            )

    def _migrate(self) -> None:
        """Additive schema migrations for databases created by older versions."""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(event)").fetchall()}
        for name, ddl in (
            ("grace_started_ts", "REAL"),
            ("grace_duration", "REAL"),
            ("last_valid_observed_ts", "REAL"),
            ("paused_ts", "REAL"),
            ("paused_seconds", "REAL NOT NULL DEFAULT 0"),
            ("pause_reason", "TEXT"),
            ("total_seconds", "REAL NOT NULL DEFAULT 576000.0"),
            ("milestones_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("continuation", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                log.info("Migrating database: adding event.%s", name)
                self._conn.execute(f"ALTER TABLE event ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------ core

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row(self) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute("SELECT * FROM event WHERE id = 1").fetchone()
        assert row is not None
        return row

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def add_unverified_seconds(self, event_uid: str, seconds: float) -> float:
        """Track time the bot was offline (VC presence could not be observed)."""
        key = f"unverified:{event_uid}"
        total = float(self.get_meta(key, "0") or 0) + max(0.0, seconds)
        self.set_meta(key, repr(total))
        return total

    def get_unverified_seconds(self, event_uid: str) -> float:
        return float(self.get_meta(f"unverified:{event_uid}", "0") or 0)

    def set_alive_check_emptied(self, event_uid: str, ts: float) -> None:
        """Remember that an alive check just emptied the VC."""
        self.set_meta(f"alive_check_emptied:{event_uid}", repr(float(ts)))

    def get_alive_check_emptied(self, event_uid: str) -> Optional[float]:
        """Check if an alive check recently emptied the VC."""
        raw = self.get_meta(f"alive_check_emptied:{event_uid}")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    def clear_alive_check_emptied(self, event_uid: str) -> None:
        """Clear the alive check emptied flag."""
        key = f"alive_check_emptied:{event_uid}"
        with self._lock:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # ----------------------------------------------------------- event state

    def load_state(self) -> EventState:
        r = self._row()
        return EventState(
            status=EventStatus(r["status"]),
            event_uid=r["event_uid"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            last_tick_ts=r["last_tick_ts"],
            guild_id=r["guild_id"],
            voice_channel_id=r["voice_channel_id"],
            announce_channel_id=r["announce_channel_id"],
            progress_channel_id=r["progress_channel_id"],
            progress_message_id=r["progress_message_id"],
            started_by=r["started_by"],
            end_reason=r["end_reason"],
            final_saved=bool(r["final_saved"]),
            grace_started_ts=r["grace_started_ts"],
            grace_duration=r["grace_duration"] if "grace_duration" in r.keys() else None,
            last_valid_observed_ts=r["last_valid_observed_ts"],
            paused_ts=r["paused_ts"],
            paused_seconds=float(r["paused_seconds"] or 0),
            pause_reason=r["pause_reason"],
            total_seconds=float(r["total_seconds"] or 576000.0),
            milestones_enabled=bool(r["milestones_enabled"]),
            continuation=bool(r["continuation"]),
        )

    def save_state(self, state: EventState) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE event SET
                    status = ?, event_uid = ?, start_ts = ?, end_ts = ?, last_tick_ts = ?,
                    guild_id = ?, voice_channel_id = ?, announce_channel_id = ?,
                    progress_channel_id = ?, progress_message_id = ?, started_by = ?,
                    end_reason = ?, final_saved = ?, grace_started_ts = ?,
                    grace_duration = ?, last_valid_observed_ts = ?, paused_ts = ?,
                    paused_seconds = ?, pause_reason = ?, total_seconds = ?,
                    milestones_enabled = ?, continuation = ?
                WHERE id = 1
                """,
                (
                    state.status.value,
                    state.event_uid,
                    state.start_ts,
                    state.end_ts,
                    state.last_tick_ts,
                    state.guild_id,
                    state.voice_channel_id,
                    state.announce_channel_id,
                    state.progress_channel_id,
                    state.progress_message_id,
                    state.started_by,
                    state.end_reason,
                    int(state.final_saved),
                    state.grace_started_ts,
                    state.grace_duration,
                    state.last_valid_observed_ts,
                    state.paused_ts,
                    state.paused_seconds,
                    state.pause_reason,
                    state.total_seconds,
                    int(state.milestones_enabled),
                    int(state.continuation),
                ),
            )

    def set_last_tick(self, ts: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE event SET last_tick_ts = ? WHERE id = 1", (ts,))

    def set_last_valid_observed_ts(self, ts: Optional[float]) -> None:
        """Persist the latest timestamp the VC was observed with valid humans.

        This is a core reconstruction field (see crash-safety docs): after a
        restart, ``last_valid_observed_ts`` tells ``resume_after_restart``
        whether the VC was recently occupied, as opposed to ``last_tick_ts``
        which is updated even on empty observations.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE event SET last_valid_observed_ts = ? WHERE id = 1", (ts,)
            )

    def set_grace_started(self, ts: Optional[float], duration: Optional[float] = None) -> None:
        """Persist the empty-VC grace window so a restart resumes it."""
        with self._lock:
            self._conn.execute(
                "UPDATE event SET grace_started_ts = ?, grace_duration = ? WHERE id = 1",
                (ts, duration if ts is not None else None),
            )

    def set_progress_message(self, channel_id: Optional[int], message_id: Optional[int]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE event SET progress_channel_id = ?, progress_message_id = ? WHERE id = 1",
                (channel_id, message_id),
            )

    # ------------------------------------------------------------ user times

    def add_user_time(self, event_uid: str, entries: Sequence[tuple[int, str, float, float]]) -> None:
        """Accumulate time.  `entries` = (user_id, display_name, delta_seconds, now_ts)."""
        if not entries:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """
                    INSERT INTO user_time(event_uid, user_id, seconds, display_name, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_uid, user_id) DO UPDATE SET
                        seconds = seconds + excluded.seconds,
                        display_name = excluded.display_name,
                        last_seen = excluded.last_seen
                    """,
                    [(event_uid, uid, delta, name, ts, ts) for uid, name, delta, ts in entries],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def touch_users(self, event_uid: str, users: Iterable[ParticipantRef], ts: float) -> None:
        """Make sure a user has a leaderboard row even with 0 accumulated time."""
        rows = [(event_uid, u.user_id, u.display_name, ts, ts) for u in users]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO user_time(event_uid, user_id, seconds, display_name, first_seen, last_seen)
                VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(event_uid, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen = excluded.last_seen
                """,
                rows,
            )

    def get_user_times(self, event_uid: str) -> list[tuple[int, str, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, display_name, seconds FROM user_time WHERE event_uid = ?",
                (event_uid,),
            ).fetchall()
        return [(r["user_id"], r["display_name"], r["seconds"]) for r in rows]

    # -------------------------------------------------------------- presence

    def replace_presence(self, event_uid: str, users: Sequence[ParticipantRef], ts: float) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM presence WHERE event_uid = ?", (event_uid,))
                self._conn.executemany(
                    "INSERT INTO presence(event_uid, user_id, display_name, since_ts) VALUES (?, ?, ?, ?)",
                    [(event_uid, u.user_id, u.display_name, ts) for u in users],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_presence(self, event_uid: str) -> list[ParticipantRef]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, display_name FROM presence WHERE event_uid = ?", (event_uid,)
            ).fetchall()
        return [ParticipantRef(r["user_id"], r["display_name"]) for r in rows]

    # ------------------------------------------------------------ milestones

    def claim_milestone(self, event_uid: str, hours: int, reached_ts: float, late: bool = False) -> bool:
        """Atomically reserve a milestone.

        Returns True only for the caller that actually inserted the row, so a
        milestone can never be triggered (or announced) twice — even if the bot
        restarts in the exact second the milestone lands.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO milestones(event_uid, hours, reached_ts, announced, late) "
                "VALUES (?, ?, ?, 0, ?)",
                (event_uid, hours, reached_ts, int(late)),
            )
            return cur.rowcount > 0

    def save_milestone_members(self, event_uid: str, hours: int, members: Sequence[ParticipantRef]) -> None:
        if not members:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO milestone_members(event_uid, hours, user_id, display_name) "
                "VALUES (?, ?, ?, ?)",
                [(event_uid, hours, m.user_id, m.display_name) for m in members],
            )

    def mark_milestone_announced(self, event_uid: str, hours: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE milestones SET announced = 1 WHERE event_uid = ? AND hours = ?",
                (event_uid, hours),
            )

    def triggered_milestone_hours(self, event_uid: str) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT hours FROM milestones WHERE event_uid = ?", (event_uid,)
            ).fetchall()
        return {r["hours"] for r in rows}

    def get_milestones(self, event_uid: str) -> list[MilestoneRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT hours, reached_ts, announced, late FROM milestones WHERE event_uid = ? ORDER BY hours",
                (event_uid,),
            ).fetchall()
            records: list[MilestoneRecord] = []
            for r in rows:
                members = self._conn.execute(
                    "SELECT user_id, display_name FROM milestone_members WHERE event_uid = ? AND hours = ?",
                    (event_uid, r["hours"]),
                ).fetchall()
                records.append(
                    MilestoneRecord(
                        hours=r["hours"],
                        reached_ts=r["reached_ts"],
                        announced=bool(r["announced"]),
                        late=bool(r["late"]),
                        members=[ParticipantRef(m["user_id"], m["display_name"]) for m in members],
                    )
                )
        return records

    def pending_announcements(self, event_uid: str) -> list[MilestoneRecord]:
        """Milestones claimed but never announced (crash between claim and post)."""
        return [m for m in self.get_milestones(event_uid) if not m.announced]

    # ----------------------------------------------------------- alive checks

    def save_alive_check(self, event_uid: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alive_check(event_uid, payload) VALUES (?, ?) "
                "ON CONFLICT(event_uid) DO UPDATE SET payload = excluded.payload",
                (event_uid, json.dumps(payload)),
            )

    def load_alive_check(self, event_uid: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM alive_check WHERE event_uid = ?", (event_uid,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover - corrupt row
            return None

    def clear_alive_check(self, event_uid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM alive_check WHERE event_uid = ?", (event_uid,))

    def record_alive_check_history(
        self,
        event_uid: str,
        *,
        check_id: str,
        started_ts: float,
        resolved_ts: float,
        required: int,
        responded: int,
        kicked: Sequence[int],
        cancelled: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alive_check_history"
                "(event_uid, check_id, started_ts, resolved_ts, required, responded, kicked, cancelled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_uid,
                    check_id,
                    started_ts,
                    resolved_ts,
                    int(required),
                    int(responded),
                    json.dumps(list(kicked)),
                    int(cancelled),
                ),
            )

    def alive_check_history(self, event_uid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alive_check_history WHERE event_uid = ? ORDER BY started_ts",
                (event_uid,),
            ).fetchall()
        return [
            {
                "check_id": r["check_id"],
                "started_ts": r["started_ts"],
                "resolved_ts": r["resolved_ts"],
                "required": r["required"],
                "responded": r["responded"],
                "kicked": json.loads(r["kicked"]),
                "cancelled": bool(r["cancelled"]),
            }
            for r in rows
        ]

    def set_next_alive_check(self, event_uid: str, ts: Optional[float]) -> None:
        key = f"next_alive_check:{event_uid}"
        if ts is None:
            with self._lock:
                self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        self.set_meta(key, repr(float(ts)))

    def get_next_alive_check(self, event_uid: str) -> Optional[float]:
        raw = self.get_meta(f"next_alive_check:{event_uid}")
        try:
            return float(raw) if raw is not None else None
        except ValueError:  # pragma: no cover
            return None

    # ------------------------------------------------------- end-of-run DMs

    def record_dm(self, event_uid: str, user_id: int, status: str, ts: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dm_log(event_uid, user_id, sent_ts, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(event_uid, user_id) DO UPDATE SET sent_ts = excluded.sent_ts, "
                "status = excluded.status",
                (event_uid, user_id, ts if ts is not None else time.time(), status),
            )

    def dm_recipients(self, event_uid: str) -> set[int]:
        """Users already messaged (any outcome) — used to resume after a restart."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM dm_log WHERE event_uid = ?", (event_uid,)
            ).fetchall()
        return {r["user_id"] for r in rows}

    def dm_recipients_by_status(self, event_uid: str) -> dict[int, str]:
        """All recipients mapped to their delivery status.

        Returns ``{user_id: status}`` so callers can tell the difference
        between ``sent``/``blocked``/``failed`` (terminal) and ``pending``
        (in-flight — should be retried on restart).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, status FROM dm_log WHERE event_uid = ?", (event_uid,)
            ).fetchall()
        return {r["user_id"]: r["status"] for r in rows}

    def dm_summary(self, event_uid: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM dm_log WHERE event_uid = ? GROUP BY status",
                (event_uid,),
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ----------------------------------------------------- final leaderboard

    def save_final_leaderboard(self, event_uid: str, entries: Sequence[LeaderboardEntry]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM final_leaderboard WHERE event_uid = ?", (event_uid,))
                self._conn.executemany(
                    "INSERT INTO final_leaderboard(event_uid, rank, position, user_id, display_name, seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (event_uid, e.rank, pos, e.user_id, e.display_name, e.seconds)
                        for pos, e in enumerate(entries, start=1)
                    ],
                )
                self._conn.execute("UPDATE event SET final_saved = 1 WHERE id = 1")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_final_leaderboard(self, event_uid: str) -> list[LeaderboardEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rank, user_id, display_name, seconds FROM final_leaderboard "
                "WHERE event_uid = ? ORDER BY position",
                (event_uid,),
            ).fetchall()
        return [LeaderboardEntry(r["rank"], r["user_id"], r["display_name"], r["seconds"]) for r in rows]

    # ----------------------------------------------------------- peak population

    def record_population(self, event_uid: str, count: int) -> int:
        """Update and return the peak human VC population observed."""
        key = f"peak_population:{event_uid}"
        current = self.get_peak_population(event_uid)
        if count > current:
            self.set_meta(key, str(count))
            return count
        return current

    def get_peak_population(self, event_uid: str) -> int:
        raw = self.get_meta(f"peak_population:{event_uid}")
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    # ----------------------------------------------------------- finale stages

    def get_finale_stages(self, event_uid: str) -> set[str]:
        raw = self.get_meta(f"finale_stages:{event_uid}")
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except json.JSONDecodeError:
            return set()

    def mark_finale_stage(self, event_uid: str, stage: str) -> None:
        stages = self.get_finale_stages(event_uid)
        stages.add(stage)
        self.set_meta(f"finale_stages:{event_uid}", json.dumps(sorted(stages)))

    # -------------------------------------------------------- continuation vote

    def get_continuation_poll(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM continuation_poll WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def set_continuation_poll(
        self,
        event_uid: str,
        *,
        channel_id: Optional[int] = None,
        message_id: Optional[int] = None,
        started_ts: Optional[float] = None,
        deadline_ts: Optional[float] = None,
        status: str = "open",
        result: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO continuation_poll(id, event_uid, status, channel_id, message_id,
                                              started_ts, deadline_ts, result)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    event_uid = excluded.event_uid,
                    status = excluded.status,
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    started_ts = excluded.started_ts,
                    deadline_ts = excluded.deadline_ts,
                    result = excluded.result
                """,
                (
                    event_uid,
                    status,
                    channel_id,
                    message_id,
                    started_ts,
                    deadline_ts,
                    result,
                ),
            )

    def clear_continuation_poll(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM continuation_poll WHERE id = 1")

    def record_continuation_vote(self, event_uid: str, user_id: int, answer: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO continuation_votes(event_uid, user_id, answer, voted_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_uid, user_id) DO UPDATE SET
                    answer = excluded.answer,
                    voted_ts = excluded.voted_ts
                """,
                (event_uid, user_id, answer, ts),
            )

    def continuation_votes(self, event_uid: str) -> dict[int, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, answer FROM continuation_votes WHERE event_uid = ?",
                (event_uid,),
            ).fetchall()
        return {r["user_id"]: r["answer"] for r in rows}

    def continuation_vote_counts(self, event_uid: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT answer, COUNT(*) AS n FROM continuation_votes WHERE event_uid = ? GROUP BY answer",
                (event_uid,),
            ).fetchall()
        counts = {"yes": 0, "no": 0}
        for row in rows:
            counts[row["answer"]] = row["n"]
        return counts

    # ------------------------------------------------------------ hell events

    def save_hell_event(
        self,
        event_uid: str,
        *,
        event_id: str,
        event_type: str,
        name: str,
        start_ts: float,
        end_ts: float,
        state: str,
        affected_users: Sequence[int] = (),
        metadata: Optional[dict] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO hell_events(
                    id, event_uid, event_type, name, start_ts, end_ts, state, affected_users, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_uid,
                    event_type,
                    name,
                    start_ts,
                    end_ts,
                    state,
                    json.dumps(list(affected_users)),
                    json.dumps(metadata or {}),
                ),
            )

    def update_hell_event(
        self,
        event_id: str,
        *,
        state: Optional[str] = None,
        end_ts: Optional[float] = None,
        affected_users: Optional[Sequence[int]] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hell_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return
            new_state = state if state is not None else row["state"]
            new_end = end_ts if end_ts is not None else row["end_ts"]
            new_users = (
                json.dumps(list(affected_users))
                if affected_users is not None
                else row["affected_users"]
            )
            new_meta = (
                json.dumps(metadata)
                if metadata is not None
                else row["metadata"]
            )
            self._conn.execute(
                """
                UPDATE hell_events SET
                    state = ?, end_ts = ?, affected_users = ?, metadata = ?
                WHERE id = ?
                """,
                (new_state, new_end, new_users, new_meta, event_id),
            )

    def get_active_hell_event(self, event_uid: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hell_events WHERE event_uid = ? AND state = 'active' ORDER BY start_ts DESC LIMIT 1",
                (event_uid,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "event_uid": row["event_uid"],
            "event_type": row["event_type"],
            "name": row["name"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "state": row["state"],
            "affected_users": json.loads(row["affected_users"]),
            "metadata": json.loads(row["metadata"]),
        }

    def get_hell_event(self, event_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hell_events WHERE id = ?", (event_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "event_uid": row["event_uid"],
            "event_type": row["event_type"],
            "name": row["name"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "state": row["state"],
            "affected_users": json.loads(row["affected_users"]),
            "metadata": json.loads(row["metadata"]),
        }

    def get_hell_events_history(self, event_uid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hell_events WHERE event_uid = ? ORDER BY start_ts DESC",
                (event_uid,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "event_uid": r["event_uid"],
                "event_type": r["event_type"],
                "name": r["name"],
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "state": r["state"],
                "affected_users": json.loads(r["affected_users"]),
                "metadata": json.loads(r["metadata"]),
            }
            for r in rows
        ]

    def set_next_hell_event(self, event_uid: str, ts: Optional[float]) -> None:
        key = f"next_hell_event:{event_uid}"
        if ts is None:
            with self._lock:
                self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        self.set_meta(key, repr(float(ts)))

    def get_next_hell_event(self, event_uid: str) -> Optional[float]:
        raw = self.get_meta(f"next_hell_event:{event_uid}")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    # ------------------------------------------------------------ maintenance

    def reset_all(self) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "user_time", "milestones", "milestone_members", "presence",
                    "final_leaderboard", "alive_check", "alive_check_history", "dm_log",
                    "hell_events", "continuation_votes",
                ):
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.execute("DELETE FROM continuation_poll WHERE id = 1")
                self._conn.execute(
                    "DELETE FROM meta WHERE key LIKE 'next_alive_check:%' OR key LIKE 'unverified:%' "
                    "OR key LIKE 'alive_check_emptied:%' OR key LIKE 'next_hell_event:%' "
                    "OR key LIKE 'peak_population:%' OR key LIKE 'finale_stages:%'"
                )
                self._conn.execute(
                    "UPDATE event SET status = ?, event_uid = NULL, start_ts = NULL, end_ts = NULL, "
                    "last_tick_ts = NULL, progress_channel_id = NULL, progress_message_id = NULL, "
                    "started_by = NULL, end_reason = NULL, final_saved = 0, "
                    "grace_started_ts = NULL, grace_duration = NULL, last_valid_observed_ts = NULL, "
                    "paused_ts = NULL, paused_seconds = 0, pause_reason = NULL, "
                    "total_seconds = 576000.0, milestones_enabled = 1, continuation = 0 WHERE id = 1",
                    (EventStatus.IDLE.value,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
