"""Container health check: is the bot actually watching the VC?

Exit code 0 = healthy, 1 = unhealthy (Docker/systemd will restart it).

A process that is alive but whose monitor loop has stalled is worse than a
crashed one — the event keeps running while nobody is watching the channel.
So this looks at the *state file*, not the process: if an event is RUNNING and
the last observation is older than the allowed lag, the bot is unhealthy.

    python tools/healthcheck.py [--max-lag SECONDS]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hell.models import EventStatus
from hell.paths import resolve

DEFAULT_MAX_LAG = 120.0


def check(database: Path, max_lag: float) -> tuple[bool, str]:
    if not database.exists():
        return True, "no database yet — the bot has not started an event"

    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        # Older databases (before the pause feature) have no paused_ts column;
        # treat them as not paused rather than failing the healthcheck.
        columns = {r["name"] for r in connection.execute("PRAGMA table_info(event)").fetchall()}
        if "paused_ts" in columns:
            row = connection.execute(
                "SELECT status, last_tick_ts, paused_ts FROM event WHERE id = 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT status, last_tick_ts, NULL AS paused_ts FROM event WHERE id = 1"
            ).fetchone()
        connection.close()
    except sqlite3.Error as exc:
        return False, f"cannot read {database}: {exc}"

    if row is None:
        return True, "no event row yet"

    status = str(row["status"])
    if status != EventStatus.RUNNING.value:
        return True, f"status {status} — nothing to watch"

    # A paused event intentionally stops ticking (the whole point is that
    # nothing runs).  Treating the stale `last_tick_ts` as a stall would make
    # Docker/systemd kill the bot mid-pause, so a paused event is healthy.
    paused_ts = row["paused_ts"] if "paused_ts" in row.keys() else None
    if paused_ts is not None:
        return True, "RUNNING but PAUSED — the timer is frozen, not stalled"

    last_tick = row["last_tick_ts"]
    if last_tick is None:
        return True, "running, waiting for the first observation"

    lag = time.time() - float(last_tick)
    if lag > max_lag:
        return False, f"the VC has not been observed for {lag:.0f}s (limit {max_lag:.0f}s)"
    return True, f"running, last observation {lag:.0f}s ago"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-lag", type=float, default=DEFAULT_MAX_LAG)
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/hell.sqlite3"))
    args = parser.parse_args()

    healthy, reason = check(resolve(args.database), args.max_lag)
    print(("healthy: " if healthy else "UNHEALTHY: ") + reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
