"""**Timeline #1 — the global event timeline: 0 → 160h.**

One clock for the whole run.  It starts at the exact `/hell start` timestamp
and ends 160 hours later, and it is the *only* thing milestones and completion
are measured against.

Deliberately independent of who is in the VC: individual users joining,
leaving, being disconnected by an alive check or being kicked as `@clanker`
never move this clock.  The single event that stops it is the run ending
(FAILED / COMPLETED / CANCELLED) — see :mod:`hell.grace` for the empty-VC rule.

For the *other* timeline — per-user accumulated time, 0 → Xh — see
:mod:`hell.tracking`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .milestones import TOTAL_SECONDS


@dataclass(frozen=True)
class EventTimeline:
    """Absolute-timestamp arithmetic for the 0 → 160h event clock."""

    start_ts: float
    total: float = TOTAL_SECONDS

    # ------------------------------------------------------------- geometry

    @property
    def deadline(self) -> float:
        """Absolute timestamp of the 160h mark."""
        return self.start_ts + self.total

    def clamp(self, now: float) -> float:
        """`now`, never past the deadline — the clock never runs beyond 160h."""
        return min(now, self.deadline)

    def elapsed(self, now: float, *, frozen_at: Optional[float] = None) -> float:
        """Elapsed event time in seconds, clamped to `[0, total]`.

        `frozen_at` is the timestamp the run stopped (fail/complete/cancel);
        once set, elapsed never moves again.
        """
        reference = frozen_at if frozen_at is not None else now
        return max(0.0, self.clamp(reference) - self.start_ts)

    def remaining(self, now: float, *, frozen_at: Optional[float] = None) -> float:
        return max(0.0, self.total - self.elapsed(now, frozen_at=frozen_at))

    def fraction(self, now: float, *, frozen_at: Optional[float] = None) -> float:
        if self.total <= 0:  # pragma: no cover - defensive
            return 0.0
        return self.elapsed(now, frozen_at=frozen_at) / self.total

    def is_finished(self, now: float) -> bool:
        """Has the 160h mark been reached?"""
        return now >= self.deadline

    def at_hours(self, hours: float) -> float:
        """Absolute timestamp of a point on the timeline (e.g. a milestone)."""
        return self.start_ts + hours * 3600.0
