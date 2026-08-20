"""Empty-VC **grace period** — the rule that decides when a run is really dead.

The event needs at least one valid human in the target VC.  When the last one
leaves, the run is *not* killed instantly: a grace window (15 seconds by
default, `EMPTY_VC_GRACE_SECONDS`) opens.

    VC becomes empty ──► GRACE OPEN ──► somebody joins in time ──► run continues
                                   └──► nobody joins ──────────► run FAILED

While the window is open:

* a **warning is posted in the progress channel with no pings at all** — the
  point is to alert whoever is already watching, not to mass-notify the server;
* the global 0 → 160h clock keeps ticking (see :mod:`hell.timeline`) but nobody
  accrues per-user time, because nobody is there;
* milestones are not triggered — an empty VC has nobody who could claim them;
* if a valid human joins before the deadline the window closes, a recovery
  notice is posted (again without pings) and the run continues normally.

If the window expires the event fails **as of the moment the VC emptied**, not
the moment the window expired, so the grace window can never inflate the
survived time.

The window is persisted (`event.grace_started_ts`), so a restart in the middle
of one resumes it instead of forgetting it.

This module is pure logic — no Discord, no database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_GRACE_SECONDS = 15.0


@dataclass
class EmptyVcGracePeriod:
    """Tracks the "VC is empty, tick tock" window for one event."""

    seconds: float = DEFAULT_GRACE_SECONDS
    empty_since: Optional[float] = None

    # --------------------------------------------------------------- state

    @property
    def is_open(self) -> bool:
        """Is the VC currently empty and inside the grace window?"""
        return self.empty_since is not None

    def deadline(self) -> Optional[float]:
        """Absolute timestamp at which the run dies, if nobody joins."""
        if self.empty_since is None:
            return None
        return self.empty_since + self.seconds

    def seconds_left(self, now: float) -> float:
        deadline = self.deadline()
        if deadline is None:
            return self.seconds
        return max(0.0, deadline - now)

    def empty_for(self, now: float) -> float:
        if self.empty_since is None:
            return 0.0
        return max(0.0, now - self.empty_since)

    # -------------------------------------------------------------- events

    def open(self, now: float) -> float:
        """Start the window (called the first tick the VC is empty)."""
        if self.empty_since is None:
            self.empty_since = now
        return self.empty_since

    def has_expired(self, now: float) -> bool:
        """Has the window run out?  (Zero-length grace fails immediately.)"""
        deadline = self.deadline()
        return deadline is not None and now >= deadline

    def close(self) -> Optional[float]:
        """Somebody joined in time — cancel the window, return when it opened."""
        started, self.empty_since = self.empty_since, None
        return started

    def restore(self, empty_since: Optional[float]) -> None:
        """Reload a window that was in progress before a restart."""
        self.empty_since = empty_since
