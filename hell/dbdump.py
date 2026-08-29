"""Full database export for the operator — the "recap it later" file.

Two artifacts are generated from a **read-only** connection (the database is
WAL-mode, so a reader can coexist with the bot's own writer: a dump can be
taken at any moment without pausing the event):

- :func:`dump_database` — the complete SQL dump via ``iterdump``: the schema
  plus every row, restorable with ``sqlite3 new.db < dump.sql``.
- :func:`build_recap` — a human-readable summary: event state, both clocks,
  milestones and their claimants, roll-call history, the continuation vote,
  last VC presence and the meta keys (gamble cooldowns/history/stats, …).

Everything is plain text/SQL on purpose: the recap must still make sense if
the bot is long gone, and the SQL dump must restore byte-for-byte.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .timeutil import format_hm

_LINE = "=" * 62


def _connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open the database read-only (uri mode=ro refuses any write)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return str(ts)


def _trunc(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def dump_database(path: str | Path) -> bytes:
    """Complete, restorable SQL dump (schema + data) of the database."""
    conn = _connect_ro(path)
    try:
        lines = [
            "-- HellBot database dump",
            f"-- source: {path}",
            f"-- generated: {_utc_now()}",
        ]
        lines.extend(conn.iterdump())
        return ("\n".join(lines) + "\n").encode("utf-8")
    finally:
        conn.close()


def build_recap(path: str | Path) -> str:
    """Human-readable recap of the whole database (point-in-time snapshot)."""
    conn = _connect_ro(path)
    try:
        return _render(conn)
    finally:
        conn.close()


def _render(conn: sqlite3.Connection) -> str:
    out: list[str] = []
    add = out.append

    add(_LINE)
    add("WELCOME TO HELL — DATABASE RECAP")
    add(f"Generated: {_utc_now()}")
    add(_LINE)

    ev = conn.execute("SELECT * FROM event WHERE id = 1").fetchone()
    uid = ev["event_uid"] if ev is not None and ev["event_uid"] else None

    _event_section(ev, out)
    _real_timer_section(conn, uid, out)
    _gamble_section(conn, uid, out)
    _milestones_section(conn, uid, out)
    _alive_checks_section(conn, uid, out)
    _continuation_section(conn, uid, out)
    _presence_section(conn, uid, out)
    _final_board_section(conn, uid, out)
    _meta_section(conn, out)

    add(_LINE)
    add("End of recap — the .sql attachment restores every byte of this database.")
    return "\n".join(out)


def _event_section(ev: Optional[sqlite3.Row], out: list[str]) -> None:
    out.append("")
    out.append("EVENT")
    if ev is None:
        out.append("  (none — the bot has never started an event)")
        return
    status = ev["status"]
    start_ts = ev["start_ts"]
    end_ts = ev["end_ts"]
    paused = float(ev["paused_seconds"] or 0.0)
    if end_ts is not None:
        elapsed = max(0.0, float(end_ts) - float(start_ts)) - paused
    elif start_ts is not None and status == "RUNNING":
        elapsed = max(0.0, time.time() - float(start_ts)) - paused
    else:
        elapsed = 0.0
    out.append(f"  Status:      {status}")
    out.append(f"  Event UID:   {ev['event_uid'] or '-'}")
    out.append(f"  Started:     {_fmt_ts(start_ts)}")
    out.append(f"  Ended:       {_fmt_ts(end_ts)}" + (f"  ({ev['end_reason']})" if ev["end_reason"] else ""))
    out.append(
        f"  Elapsed:     {format_hm(elapsed)} of {format_hm(ev['total_seconds'] or 0.0)}"
        f"  (paused time excluded: {format_hm(paused)})"
    )
    if ev["pause_reason"]:
        out.append(f"  Last pause:  {ev['pause_reason']}")
    out.append(f"  Continuation (Hell 2): {'yes' if ev['continuation'] else 'no'}"
               f"  · milestones: {'on' if ev['milestones_enabled'] else 'off'}"
               f"  · final saved: {'yes' if ev['final_saved'] else 'no'}")
    channels = (
        f"  Guild/VC/announce: {ev['guild_id'] or '-'}/{ev['voice_channel_id'] or '-'}/"
        f"{ev['announce_channel_id'] or '-'}"
    )
    out.append(channels)


def _real_timer_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT user_id, display_name, seconds, first_seen, last_seen "
        "FROM user_time WHERE event_uid = ? ORDER BY seconds DESC, user_id",
        (uid,),
    ).fetchall() if uid else []
    out.append("")
    out.append(f"REAL TIMER LEADERBOARD ({len(rows)} contestants)")
    if not rows:
        out.append("  (empty)")
        return
    for rank, row in enumerate(rows, 1):
        out.append(
            f"  {rank:>3}. {row['display_name'] or row['user_id']}  <@{row['user_id']}>  "
            f"{format_hm(row['seconds'])}   (last seen {_fmt_ts(row['last_seen'])})"
        )


def _gamble_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT user_id, display_name, wallet, net FROM gamble_time "
        "WHERE event_uid = ? ORDER BY wallet DESC, user_id",
        (uid,),
    ).fetchall() if uid else []
    out.append("")
    out.append(f"GAMBLE TIME ({len(rows)} wallets)")
    if not rows:
        out.append("  (nobody has a wallet yet)")
        return
    for row in rows:
        sign = "+" if row["net"] >= 0 else "-"
        out.append(
            f"  {row['display_name'] or row['user_id']}  <@{row['user_id']}>  "
            f"wallet {format_hm(row['wallet'])}  ·  net {sign}{format_hm(abs(row['net']))}"
        )


def _milestones_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT hours, reached_ts, announced, late FROM milestones "
        "WHERE event_uid = ? ORDER BY hours",
        (uid,),
    ).fetchall() if uid else []
    out.append("")
    out.append(f"MILESTONES ({len(rows)} reached)")
    if not rows:
        out.append("  (none)")
        return
    for row in rows:
        members = conn.execute(
            "SELECT display_name, user_id FROM milestone_members "
            "WHERE event_uid = ? AND hours = ? ORDER BY user_id",
            (uid, row["hours"]),
        ).fetchall()
        names = ", ".join(m["display_name"] or str(m["user_id"]) for m in members[:10])
        more = f" +{len(members) - 10} more" if len(members) > 10 else ""
        flags = []
        if row["late"]:
            flags.append("late")
        if not row["announced"]:
            flags.append("UNANNOUNCED")
        out.append(
            f"  {row['hours']:>3}h  at {_fmt_ts(row['reached_ts'])}"
            f"{'  [' + ', '.join(flags) + ']' if flags else ''}"
        )
        if members:
            out.append(f"        claimants ({len(members)}): {names}{more}")


def _alive_checks_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT started_ts, resolved_ts, required, responded, kicked, cancelled "
        "FROM alive_check_history WHERE event_uid = ? ORDER BY started_ts DESC",
        (uid,),
    ).fetchall() if uid else []
    out.append("")
    out.append(f"ALIVE CHECK HISTORY ({len(rows)} checks)")
    if not rows:
        out.append("  (none)")
        return
    for row in rows[:50]:
        try:
            kicked = json.loads(row["kicked"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            kicked = []
        state = "CANCELLED" if row["cancelled"] else (
            f"{row['responded']}/{row['required']} replied, {len(kicked)} kicked"
        )
        out.append(
            f"  {_fmt_ts(row['started_ts'])}  → {_fmt_ts(row['resolved_ts'])}  ·  {state}"
        )
    if len(rows) > 50:
        out.append(f"  … and {len(rows) - 50} older checks (see the .sql dump)")


def _continuation_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    poll = conn.execute(
        "SELECT * FROM continuation_poll WHERE id = 1" + (" AND event_uid = ?" if uid else ""),
        (uid,) if uid else (),
    ).fetchone()
    votes = conn.execute(
        "SELECT answer, COUNT(*) AS n FROM continuation_votes"
        + (" WHERE event_uid = ?" if uid else "") + " GROUP BY answer",
        (uid,) if uid else (),
    ).fetchall()
    out.append("")
    out.append("CONTINUATION VOTE (Hell 2 → 320h)")
    if poll is None and not votes:
        out.append("  (no poll)")
        return
    if poll is not None:
        out.append(
            f"  Status: {poll['status']}  ·  started {_fmt_ts(poll['started_ts'])}"
            f"  ·  deadline {_fmt_ts(poll['deadline_ts'])}"
        )
    if votes:
        yes = next((v["n"] for v in votes if v["answer"] == "yes"), 0)
        no = next((v["n"] for v in votes if v["answer"] == "no"), 0)
        result = f"  ·  result: {poll['result']}" if (poll and poll["result"]) else ""
        out.append(f"  Votes: {yes} yes / {no} no{result}")


def _presence_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM presence WHERE event_uid = ?", (uid,)
    ).fetchone() if uid else None
    members = (
        conn.execute(
            "SELECT user_id, display_name FROM presence WHERE event_uid = ? ORDER BY user_id",
            (uid,),
        ).fetchall()[:10]
        if uid
        else []
    )
    out.append("")
    out.append(f"LAST VC PRESENCE ({rows['n'] if rows else 0} observed)")
    names = [r["display_name"] or str(r["user_id"]) for r in members]
    if names:
        more = "" if (rows and rows["n"] <= 10) else " …"
        out.append(f"  {', '.join(names)}{more}")
    else:
        out.append("  (none recorded)")


def _final_board_section(conn: sqlite3.Connection, uid: Optional[str], out: list[str]) -> None:
    rows = conn.execute(
        "SELECT rank, display_name, user_id, seconds FROM final_leaderboard "
        "WHERE event_uid = ? ORDER BY position",
        (uid,),
    ).fetchall() if uid else []
    if not rows:
        return
    out.append("")
    out.append(f"FINAL LEADERBOARD (frozen, {len(rows)} rows)")
    for row in rows[:20]:
        out.append(
            f"  {row['rank']:>3}. {row['display_name'] or row['user_id']}  "
            f"<@{row['user_id']}>  {format_hm(row['seconds'])}"
        )
    if len(rows) > 20:
        out.append(f"  … and {len(rows) - 20} more (see the .sql dump)")


def _meta_section(conn: sqlite3.Connection, out: list[str]) -> None:
    rows = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    out.append("")
    out.append(f"META KEYS ({len(rows)}) — gamble cooldowns/history/stats, schema version, …")
    if not rows:
        out.append("  (none)")
        return
    for row in rows[:200]:
        out.append(f"  {row['key']} = {_trunc(str(row['value']))}")
    if len(rows) > 200:
        out.append(f"  … and {len(rows) - 200} more keys (see the .sql dump)")
