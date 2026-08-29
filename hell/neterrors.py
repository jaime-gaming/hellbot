"""Uniform handling of transient "Discord unreachable" failures.

``discord.HTTPException`` covers Discord *answering* with an HTTP error code;
raw ``aiohttp.ClientError`` / timeouts cover the server *not answering at all*
(DNS failure, egress down, connection refused/reset, TLS drop). During a
network outage the bot must keep running and retry silently — these helpers
give every call site the same exception tuple and the same
log-once-per-outage behaviour, instead of a fresh traceback every 20 seconds.

These are transient by definition: the event is never affected (state lives in
SQLite and the timer is wall-clock), so the right reaction is a single clear
ERROR when the outage starts, quiet retries while it lasts, and an INFO when
connectivity returns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger("hell.net")

#: Exceptions meaning "Discord is unreachable right now" (transient — retry).
UNREACHABLE: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,     # DNS failure, refused/reset, TLS drop, HTTP-layer timeouts
    asyncio.TimeoutError,    # request timed out (== TimeoutError on 3.11+)
    OSError,                 # raw socket errors that escape the aiohttp wrappers
)


class OutageTracker:
    """Log the first failure of an outage loudly, the rest quietly.

    ``note_failure``: one ERROR (with the underlying exception and an
    actionable hint) when the outage starts, DEBUG on every retry after.
    ``note_success``: one INFO with the outage duration, then reset.
    """

    def __init__(self, label: str):
        self.label = label
        self._since: Optional[float] = None
        self._count = 0

    @property
    def in_outage(self) -> bool:
        return self._since is not None

    def note_failure(self, exc: BaseException) -> None:
        self._count += 1
        now = time.monotonic()
        if self._since is None:
            self._since = now
            log.error(
                "%s: Discord unreachable (%s: %s). Retrying automatically — the "
                "event is NOT affected (state is persisted, the timer keeps "
                "running). If this lasts more than a couple of minutes, check "
                "the server's internet connection (DNS/egress).",
                self.label,
                type(exc).__name__,
                str(exc)[:300],
            )
        else:
            log.debug(
                "%s: still unreachable (attempt %d, %.0fs into the outage)",
                self.label,
                self._count,
                now - self._since,
            )

    def note_success(self) -> None:
        if self._since is not None:
            log.info(
                "%s: Discord reachable again after %.0fs (%d failed attempt(s))",
                self.label,
                time.monotonic() - self._since,
                self._count,
            )
        self._since = None
        self._count = 0
