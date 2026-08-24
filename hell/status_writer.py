"""Write docs/status.json from the bot's internal state.

Called by the bot on every heartbeat (default every 15 minutes) and on every
state transition (start, stop, pause, resume, reset, failure, completion).
This file is tracked in git so GitHub Pages serves it to static visitors.

Usage from the bot:
    from hell.status_writer import write_status
    write_status("docs/status.json", engine=engine, monitor=monitor, stream=log_stream, bot=bot)
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .timeutil import format_hms


class StatusFile:
    """Holds a rolling window of errors and warnings, written to status.json.

    One instance lives on the HellBot object. The heartbeat loop and state
    transitions call ``.write("docs/status.json", engine=..., monitor=...)``
    to persist the live event state for GitHub Pages.
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
        paused: bool = False,
        pause_reason: Optional[str] = None,
        end_reason: Optional[str] = None,
        participants: int = 0,
        elapsed_seconds: float = 0.0,
        total_seconds: float = 576000.0,
        remaining_seconds: float = 576000.0,
        fraction: float = 0.0,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        current_milestone: Optional[dict[str, Any]] = None,
        upcoming_milestone: Optional[dict[str, Any]] = None,
        leaderboard: Optional[list[dict[str, Any]]] = None,
        leaderboard_total: int = 0,
        grace_open: bool = False,
        grace_seconds_left: float = 0.0,
        grace_total: float = 15.0,
        alive_check: Optional[dict[str, Any]] = None,
        health_errors: Optional[list[str]] = None,
        health_warnings: Optional[list[str]] = None,
        health_info: Optional[list[str]] = None,
        blind_seconds: float = 0.0,
        active_tasks: int = 0,
        version: str = "1.0.0",
    ) -> dict[str, Any]:
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
        if health_errors:
            for err in health_errors:
                if err not in recent_errors:
                    recent_errors.append(err)
        if health_warnings:
            for warn in health_warnings:
                if warn not in recent_warnings:
                    recent_warnings.append(warn)

        self._last_update = now
        return {
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ts": now,
            "bot_connected": bot_connected,
            "event_status": event_status,
            "status": event_status,
            "paused": paused,
            "pause_reason": pause_reason,
            "end_reason": end_reason,
            "event_elapsed_hours": round(event_elapsed_hours, 2),
            "elapsed_seconds": round(elapsed_seconds, 1),
            "total_seconds": round(total_seconds, 1),
            "remaining_seconds": round(remaining_seconds, 1),
            "fraction": round(fraction, 4),
            "participants": participants,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "grace_open": grace_open,
            "grace_seconds_left": round(grace_seconds_left, 1),
            "grace_total": round(grace_total, 1),
            "current_milestone": current_milestone,
            "upcoming_milestone": upcoming_milestone,
            "leaderboard": leaderboard or [],
            "leaderboard_total": leaderboard_total,
            "alive_check_pending": alive_check_pending,
            "alive_check": alive_check,
            "errors_last_24h": recent_errors[-20:],
            "warnings_last_24h": recent_warnings[-20:],
            "health_errors": recent_errors[-20:],
            "health_warnings": recent_warnings[-20:],
            "health_info": health_info or [],
            "rate_limits_5min": rate_limits_5min,
            "monitor_stale_seconds": round(monitor_stale_seconds, 1) if monitor_stale_seconds is not None else None,
            "blind_seconds": round(blind_seconds, 1),
            "active_tasks": active_tasks,
            "operator_dm_ok": operator_dm_ok,
            "version": version,
        }

    def clear_stale_errors(self) -> None:
        """Self-healing: automatically clear resolved error conditions.

        Errors older than 1 hour are dropped when the event is running and
        the monitor reports no problems. This keeps the dashboard tidy
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

    def write(self, path: str | Path, *, engine=None, monitor=None, stream=None, bot=None) -> None:
        """Convenience: snapshot and write to a JSON file in one call."""
        if engine is None:
            self.clear_stale_errors()
            payload = self.snapshot(bot_connected=False, event_status="IDLE")
        else:
            now = time.time()
            snap = engine.snapshot(now=now)
            elapsed = engine.elapsed(now=now)
            sec = monitor.security if (monitor and hasattr(monitor, "security")) else None
            security_snapshot = sec.snapshot() if sec else {}
            # Self-healing: clear old errors when the event is running fine
            if engine.status.is_active:
                stale = security_snapshot.get("stale_seconds")
                if stale is None or stale < 300:
                    self.clear_stale_errors()

            # Leaderboard
            board = engine.leaderboard()
            lb: list[dict[str, Any]] = []
            for e in board[:20]:
                lb.append({
                    "rank": e.rank,
                    "name": e.display_name,
                    "seconds": round(e.seconds, 1),
                    "time": format_hms(e.seconds),
                })

            # Milestones
            current_ms = None
            if snap.current:
                current_ms = {
                    "hours": snap.current.hours,
                    "title": snap.current.title,
                    "short_reward": snap.current.short_reward or snap.current.reward,
                }

            upcoming_ms = None
            if snap.upcoming:
                upcoming_ms = {
                    "hours": snap.upcoming.hours,
                    "title": snap.upcoming.title,
                    "short_reward": snap.upcoming.short_reward or snap.upcoming.reward,
                    "time_to": round(snap.time_to_next, 1) if snap.time_to_next is not None else None,
                }

            # Alive check
            alive_info = None
            alive_pending = False
            if monitor and hasattr(monitor, "alive_checks") and monitor.alive_checks.pending is not None:
                ac = monitor.alive_checks.pending
                alive_pending = True
                alive_info = {
                    "answered": len(ac.responded),
                    "total": len(ac.required),
                    "seconds_left": max(0, int(ac.deadline_ts - now)),
                }

            # Participants count
            vc_count = snap.participants
            if monitor and hasattr(monitor, "engine"):
                vc_count = len(monitor.engine.last_participants)

            # Health data from bot if available
            health_errors: list[str] = []
            health_warnings: list[str] = []
            health_info: list[str] = []
            if bot and getattr(bot, "health", None):
                health_errors = list(bot.health.errors)
                health_warnings = list(bot.health.warnings)
                health_info = list(bot.health.info)

            # Active tasks count
            from .tasks import active as active_tasks_count
            task_count = active_tasks_count()

            # Version
            from . import __version__

            blind_sec = monitor.blind_seconds if (monitor and hasattr(monitor, "blind_seconds")) else 0.0

            payload = self.snapshot(
                bot_connected=True,
                event_status=engine.status.value if engine.status else "IDLE",
                event_elapsed_hours=elapsed / 3600.0 if elapsed else 0.0,
                elapsed_seconds=snap.elapsed,
                total_seconds=snap.total,
                remaining_seconds=snap.remaining,
                fraction=snap.fraction,
                start_ts=snap.start_ts,
                end_ts=snap.end_ts,
                paused=snap.paused,
                pause_reason=engine.state.pause_reason,
                end_reason=engine.state.end_reason,
                participants=vc_count,
                grace_open=snap.grace_open,
                grace_seconds_left=snap.grace_seconds_left,
                grace_total=snap.grace_total,
                current_milestone=current_ms,
                upcoming_milestone=upcoming_ms,
                leaderboard=lb,
                leaderboard_total=len(board),
                alive_check_pending=alive_pending,
                alive_check=alive_info,
                rate_limits_5min=security_snapshot.get("rate_limits_5min", 0),
                monitor_stale_seconds=security_snapshot.get("stale_seconds"),
                blind_seconds=blind_sec,
                active_tasks=task_count,
                operator_dm_ok=stream is None or stream.enabled,
                health_errors=health_errors,
                health_warnings=health_warnings,
                health_info=health_info,
                version=__version__,
            )

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")

    @staticmethod
    def _ts_from_line(line: str) -> Optional[float]:
        """Parse the ISO-ish timestamp we prepend, or None."""
        try:
            date_str = line[1:20]
            return time.mktime(time.strptime(date_str, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, IndexError):
            return None


_STATUS_FILE: Optional[StatusFile] = None


def get_status() -> StatusFile:
    global _STATUS_FILE
    if _STATUS_FILE is None:
        _STATUS_FILE = StatusFile()
    return _STATUS_FILE


def write_status(path: str = "docs/status.json", *, engine=None, monitor=None, stream=None, bot=None) -> None:
    get_status().write(path, engine=engine, monitor=monitor, stream=stream, bot=bot)
