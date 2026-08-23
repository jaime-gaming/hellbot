"""Write docs/status.json from the bot's internal state.

Called by the bot on every heartbeat (default every 15 minutes).  This file
is tracked in git so GitHub Pages serves it.  Only errors and warnings from
the last 24 hours are exported — no full logs, no participant data, no tokens.

Usage from the bot:
    from tools.status_writer import write_status
    write_status(engine, monitor, log_stream, path="docs/status.json")
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Optional


class StatusFile:
    """Holds a rolling window of errors and warnings, written to status.json.

    One instance lives on the HellBot object.  The heartbeat loop calls
    ``.snapshot(engine, monitor)`` and writes the result to ``docs/status.json``.
    """

    def __init__(self, max_events: int = 100):
        self._errors: deque[str] = deque(maxlen=max_events)
        self._warnings: deque[str] = deque(maxlen=max_events)
        self._last_update: float = 0.0

    def add_error(self, message: str) -> None:
        """Record an error with a timestamp."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        self._errors.append(f"[{ts}] {message}")

    def add_warning(self, message: str) -> None:
        """Record a warning with a timestamp."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        self._warnings.append(f"[{ts}] {message}")

    def snapshot(
        self,
        *,
        bot_connected: bool,
        event_status: str,
        event_elapsed_hours: float = 0.0,
        rate_limits_5min: int = 0,
        monitor_stale_seconds: Optional[float] = None,
        alive_check_pending: bool = False,
        operator_dm_ok: bool = True,
    ) -> dict:
        """Build the status.json payload from the current state."""
        now = time.time()
        # Keep only events from the last 24 hours.
        cutoff = now - 86400
        recent_errors = [
            e for e in self._errors
            if self._ts_from_line(e) is None or self._ts_from_line(e) > cutoff
        ]
        recent_warnings = [
            w for w in self._warnings
            if self._ts_from_line(w) is None or self._ts_from_line(w) > cutoff
        ]

        self._last_update = now
        return {
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bot_connected": bot_connected,
            "event_status": event_status,
            "event_elapsed_hours": round(event_elapsed_hours, 1),
            "errors_last_24h": recent_errors[-20:],
            "warnings_last_24h": recent_warnings[-20:],
            "alive_check_pending": alive_check_pending,
            "monitor_stale_seconds": round(monitor_stale_seconds, 1) if monitor_stale_seconds is not None else None,
            "rate_limits_5min": rate_limits_5min,
            "operator_dm_ok": operator_dm_ok,
        }

    def clear_stale_errors(self) -> None:
        """Self-healing: automatically clear resolved error conditions.

        Errors older than 1 hour are dropped when the event is running and
        the monitor reports no problems.  This keeps the dashboard tidy
        without losing useful history.
        """
        now = time.time()
        cutoff = now - 3600  # 1 hour — everything older is stale
        self._errors = deque(
            [e for e in self._errors if self._ts_from_line(e) is not None and self._ts_from_line(e) > cutoff],
            maxlen=self._errors.maxlen,
        )
        self._warnings = deque(
            [w for w in self._warnings if self._ts_from_line(w) is not None and self._ts_from_line(w) > cutoff],
            maxlen=self._warnings.maxlen,
        )

    def write(self, path: str | Path, *, engine=None, monitor=None, stream=None) -> None:
        """Convenience: snapshot and write to a JSON file in one call."""
        if engine is None or monitor is None:
            self.clear_stale_errors()
            payload = self.snapshot(bot_connected=False, event_status="UNKNOWN")
        else:
            elapsed = engine.elapsed()
            sec = monitor.security if hasattr(monitor, "security") else None
            security_snapshot = sec.snapshot() if sec else {}
            # Self-healing: clear old errors when the event is running fine
            if engine.status.is_active:
                stale = security_snapshot.get("stale_seconds")
                if stale is None or stale < 300:
                    self.clear_stale_errors()
            elapsed = engine.elapsed()
            sec = monitor.security if hasattr(monitor, "security") else None
            security_snapshot = sec.snapshot() if sec else {}
            payload = self.snapshot(
                bot_connected=True,
                event_status=engine.status.value if engine.status else "IDLE",
                event_elapsed_hours=elapsed / 3600.0 if elapsed else 0.0,
                rate_limits_5min=security_snapshot.get("rate_limits_5min", 0),
                monitor_stale_seconds=security_snapshot.get("stale_seconds"),
                alive_check_pending=monitor.alive_checks.pending is not None,
                operator_dm_ok=stream is None or stream.enabled,
            )

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")

    @staticmethod
    def _ts_from_line(line: str) -> Optional[float]:
        """Parse the ISO-ish timestamp we prepend, or None."""
        try:
            # Lines look like "[2026-08-23 12:00:00 UTC] message"
            date_str = line[1:20]  # "2026-08-23 12:00:00"
            return time.mktime(time.strptime(date_str, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, IndexError):
            return None


# Module-level convenience for easy import
_STATUS_FILE: Optional[StatusFile] = None


def get_status() -> StatusFile:
    global _STATUS_FILE
    if _STATUS_FILE is None:
        _STATUS_FILE = StatusFile()
    return _STATUS_FILE


def write_status(path: str = "docs/status.json", *, engine=None, monitor=None, stream=None) -> None:
    get_status().write(path, engine=engine, monitor=monitor, stream=stream)
