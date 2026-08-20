"""**Timeline #2 — per-user session time: 0 → Xh.**

Every human accumulates their own total of seconds spent inside the target VC
while the event is RUNNING.  These per-user clocks are completely separate from
the global 0 → 160h event timeline (:mod:`hell.timeline`):

* a user leaving only stops **their** clock — the event clock keeps running as
  long as at least one valid human remains (and, with the grace period, even
  through a short gap);
* a user rejoining resumes accumulating on top of their existing total, it
  never resets;
* being disconnected (alive check, `@clanker`, moderation) never removes time
  that was already earned;
* bots and `@clanker` users never have a clock at all — they are filtered out
  before they reach this module.

Credit is granted per observation, capped by `max_tick_credit`, so a gap in
observations (bot downtime) can never be silently handed out as VC time.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .leaderboard import build_leaderboard
from .models import LeaderboardEntry, ParticipantRef
from .storage import Store

log = logging.getLogger("hell.tracking")


@dataclass(frozen=True)
class CreditResult:
    """Outcome of crediting one observation interval."""

    credited: float      # seconds actually awarded to each present user
    unverified: float    # seconds that passed unobserved (never credited)
    users: int           # how many users received the credit


class UserTimeTracker:
    """Owns the per-user 0 → Xh clocks and the leaderboard they feed."""

    def __init__(self, store: Store, *, max_credit: float = 5.0):
        self.store = store
        self.max_credit = max_credit

    def credit(
        self,
        event_uid: str,
        *,
        previous_ts: float,
        now_ts: float,
        participants: Sequence[ParticipantRef],
        stamp: float | None = None,
    ) -> CreditResult:
        """Advance every present user's clock by the observed interval."""
        raw_delta = max(0.0, now_ts - previous_ts)
        credited = min(raw_delta, self.max_credit)
        unverified = raw_delta - credited
        stamp = now_ts if stamp is None else stamp

        if unverified > 0.5:
            total = self.store.add_unverified_seconds(event_uid, unverified)
            log.warning(
                "Observation gap of %.1fs (bot downtime?) — not credited; total unverified %.1fs",
                unverified,
                total,
            )

        if participants:
            if credited > 0:
                self.store.add_user_time(
                    event_uid,
                    [(p.user_id, p.display_name, credited, stamp) for p in participants],
                )
            else:
                # Still make sure everyone present has a leaderboard row.
                self.store.touch_users(event_uid, participants, stamp)

        return CreditResult(credited=credited, unverified=unverified, users=len(participants))

    # ------------------------------------------------------------- readers

    def totals(self, event_uid: str) -> list[LeaderboardEntry]:
        """Ranked per-user totals (0 → Xh each), highest first."""
        return build_leaderboard(self.store.get_user_times(event_uid))
