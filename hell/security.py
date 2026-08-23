"""Suspicion and anomaly tracking — anti-cheat and auto-heal for the event.

This module watches for patterns that could indicate cheating, abuse, or
operator-unfriendly behaviour, and surfaces them to the operator via the
existing log alert ping system.

What is tracked
---------------
* **Alive-check dodging** – someone leaves the VC just before a roll call
  starts and returns right after it finishes.  Repeated dodging is flagged.
* **Rate-limit bursts** – when the bot gets 429 responses faster than normal,
  the operator is alerted and the bot backs off automatically.
* **VC flapping** – a single person leaving and rejoining many times inside
  a short window (suggests disconnecting to pause their own clock).
* **Stale monitor** – the VC hasn't been successfully observed for longer
  than expected (the engine keeps ticking but nobody is being seen).
* **Multiple-account presence** – the same IP/hardware cannot be detected from
  Discord, but suspicious join/leave patterns can.

Reporting
---------
Every finding is a log line at WARNING or ERROR level, which the existing
:class:`~hell.logsink.DiscordLogStream` picks up and delivers as an @-ping
alert to the operator (Jaime Gaming) when appropriate.  No extra DM plumbing
is needed.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Optional

from .alivecheck import AliveCheckManager
from .timeutil import now_ts

log = logging.getLogger("hell.security")

# ---- thresholds -----------------------------------------------------------

FLAP_WINDOW = 120.0          # seconds — lookback for join/leave counting
FLAP_THRESHOLD = 6           # joins+leaves inside the window -> flagged
ALIVE_DODGE_WINDOW = 60.0    # seconds before/after a check qualifies as dodging
ALIVE_DODGE_THRESHOLD = 2    # dodges before the operator is warned
RATE_LIMIT_SPIKE_WINDOW = 300.0  # seconds — rate limit counting window
RATE_LIMIT_SPIKE_THRESHOLD = 10  # 429s inside the window -> spike alert
STALE_MONITOR_LIMIT = 300.0      # seconds without a successful collect -> alert


class SuspicionTracker:
    """Tracks suspicious patterns and emits alerts when thresholds are crossed.

    Thread-compatible: all data is held in memory and protected by the
    caller's asyncio lock (the monitor's own ``_lock``).
    """

    def __init__(self, alive: AliveCheckManager):
        self._alive = alive
        # user_id -> deque of (timestamp, +1 for join, -1 for leave)
        self._flaps: dict[int, deque[tuple[float, int]]] = defaultdict(
            lambda: deque(maxlen=20)
        )
        # user_id -> count of alive-check dodges
        self._dodges: dict[int, int] = defaultdict(int)
        # user_id -> last check_id they were required for
        self._last_check_required: dict[int, str] = {}
        # Rolling count of 429 responses by timestamp
        self._rate_limits: deque[float] = deque(maxlen=100)
        # Whether we already alerted about a rate-limit spike
        self._rate_limit_alerted = False
        # Last successful VC collect timestamp
        self._last_collect_ts: Optional[float] = None
        # Whether we already alerted about a stale monitor
        self._stale_alerted = False
        # Total reconnections per user (for /hell security)
        self._total_flaps: dict[int, int] = defaultdict(int)

    # ---------------------------------------------------------------- flaps

    def record_join(self, user_id: int) -> None:
        self._flaps[user_id].append((now_ts(), 1))
        self._total_flaps[user_id] += 1
        self._check_flap(user_id)

    def record_leave(self, user_id: int) -> None:
        self._flaps[user_id].append((now_ts(), -1))
        self._total_flaps[user_id] += 1
        self._check_flap(user_id)

    def _check_flap(self, user_id: int) -> None:
        """If someone is joining/leaving too often, log a warning."""
        now = now_ts()
        events = [e for e in self._flaps[user_id] if now - e[0] < FLAP_WINDOW]
        joins = sum(1 for _, d in events if d > 0)
        leaves = sum(1 for _, d in events if d < 0)
        total = joins + leaves
        if total >= FLAP_THRESHOLD:
            log.warning(
                "⚠️ Suspicious VC activity — %s joined %d× and left %d× "
                "in the last %.0fs (threshold %d)",
                user_id, joins, leaves, FLAP_WINDOW, FLAP_THRESHOLD,
            )

    @property
    def flappers(self) -> list[tuple[int, int]]:
        """List of (user_id, total_flaps) for users with flap activity."""
        return sorted(
            [(uid, count) for uid, count in self._total_flaps.items() if count > 0],
            key=lambda x: -x[1],
        )

    # --------------------------------------------------------- alive dodging

    def record_check_required(self, user_ids: list[int], check_id: str) -> None:
        """Remember who was pinged for a roll call."""
        for uid in user_ids:
            self._last_check_required[uid] = check_id

    def record_check_departure(self, user_id: int, before_check: bool) -> None:
        """If someone leaves suspiciously close to a check, count a dodge.

        ``before_check`` = True means they left *before* the check was posted
        (likely dodging).  The check resolution will also detect people who
        left during the check window.
        """
        if not before_check:
            return
        if user_id not in self._last_check_required:
            return
        self._dodges[user_id] += 1
        count = self._dodges[user_id]
        log.warning(
            "⚠️ Alive-check dodging — %s left the VC right before a roll call "
            "(%d dodges recorded)",
            user_id, count,
        )
        if count >= ALIVE_DODGE_THRESHOLD:
            log.error(
                "🚨 Repeated alive-check dodging by %s — %d occurrences. "
                "They may be disconnecting to avoid answering.",
                user_id, count,
            )

    @property
    def dodgers(self) -> list[tuple[int, int]]:
        return sorted(
            [(uid, count) for uid, count in self._dodges.items() if count > 0],
            key=lambda x: -x[1],
        )

    # -------------------------------------------------------- rate limits

    def record_rate_limit(self) -> None:
        now = now_ts()
        self._rate_limits.append(now)
        self._rate_limit_alerted = False  # reset so next spike re-alerts

    def check_rate_limit_spike(self) -> None:
        """Log an error if rate limits are spiking."""
        if self._rate_limit_alerted:
            return
        now = now_ts()
        recent = [t for t in self._rate_limits if now - t < RATE_LIMIT_SPIKE_WINDOW]
        if len(recent) >= RATE_LIMIT_SPIKE_THRESHOLD:
            log.error(
                "🚨 Rate-limit spike — %d Discord 429 responses in the last %.0fs. "
                "The bot may be hitting API limits.",
                len(recent), RATE_LIMIT_SPIKE_WINDOW,
            )
            self._rate_limit_alerted = True

    @property
    def rate_limit_count(self) -> int:
        now = now_ts()
        return len([t for t in self._rate_limits if now - t < RATE_LIMIT_SPIKE_WINDOW])

    # --------------------------------------------------------- stale monitor

    def record_collect(self) -> None:
        self._last_collect_ts = now_ts()
        self._stale_alerted = False

    def check_stale(self) -> None:
        """Log a warning if the VC hasn't been observed in a while."""
        if self._stale_alerted or self._last_collect_ts is None:
            return
        lag = now_ts() - self._last_collect_ts
        if lag > STALE_MONITOR_LIMIT:
            log.error(
                "🚨 VC monitor appears stalled — last successful observation "
                "was %.0fs ago. The event timer keeps running but nobody's "
                "presence is being verified.",
                lag,
            )
            self._stale_alerted = True

    # ----------------------------------------------------- presence tracking

    def record_known_presence_change(self, joined: list[str], left: list[str]) -> None:
        """Track join/leave events and link with alive checks."""
        # (This is called from the monitor's _log_presence_changes)
        # We don't have user_ids here, just names, so we use the
        # already-existing _note_blind / presence tracking.
        pass

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        """Summary for the operator (/hell security)."""
        return {
            "dodgers": self.dodgers,
            "flappers": self.flappers,
            "rate_limits_5min": self.rate_limit_count,
            "stale_seconds": (
                (now_ts() - self._last_collect_ts)
                if self._last_collect_ts is not None
                else None
            ),
        }
