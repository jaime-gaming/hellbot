"""Milestone table — structure only; all wording lives in `Announcements.py`.

Milestones are evaluated against the **global event timer** (0 → 160h) only;
individual user time never influences them.
"""

from __future__ import annotations

from typing import Optional

from .models import Milestone
from .texts import TEXT

# Rebuilt in place by `refresh()` so that `from .milestones import MILESTONES`
# keeps working after a hot reload of Announcements.py.
MILESTONES: list[Milestone] = []
_BY_HOURS: dict[int, Milestone] = {}

TOTAL_SECONDS: float = 160 * 3600.0
FINAL_MILESTONE_HOURS: int = 160


def _build() -> list[Milestone]:
    out: list[Milestone] = []
    for entry in TEXT.MILESTONES:
        out.append(
            Milestone(
                hours=int(entry["hours"]),
                title=str(entry["title"]),
                reward=str(entry["reward"]),
                blurb=str(entry["blurb"]),
                short_reward=str(entry.get("short_reward", "")),
                flavour=str(entry.get("flavour", "")),
            )
        )
    return sorted(out, key=lambda m: m.hours)


def refresh() -> list[Milestone]:
    """Rebuild the table from `Announcements.py` (called after a hot reload)."""
    global TOTAL_SECONDS, FINAL_MILESTONE_HOURS
    rebuilt = _build()
    MILESTONES[:] = rebuilt
    _BY_HOURS.clear()
    _BY_HOURS.update({m.hours: m for m in rebuilt})
    if rebuilt:
        FINAL_MILESTONE_HOURS = rebuilt[-1].hours
        TOTAL_SECONDS = float(FINAL_MILESTONE_HOURS * 3600)
    return MILESTONES


refresh()


# Kept as module attributes for readability at call sites.
TOP3_BONUS_ROLE = TEXT.TOP3_BONUS_ROLE
VERITIES_URL = TEXT.VERITIES_URL


def get_milestone(hours: int) -> Milestone:
    return _BY_HOURS[hours]


def milestone_hours() -> tuple[int, ...]:
    return tuple(m.hours for m in MILESTONES)


def milestones_reached_at(elapsed_seconds: float) -> tuple[Milestone, ...]:
    """Every milestone whose threshold is covered by `elapsed_seconds`."""
    return tuple(m for m in MILESTONES if elapsed_seconds >= m.seconds)


def current_milestone(elapsed_seconds: float) -> Optional[Milestone]:
    """Highest milestone already reached, or None."""
    reached = milestones_reached_at(elapsed_seconds)
    return reached[-1] if reached else None


def next_milestone(elapsed_seconds: float) -> Optional[Milestone]:
    """Lowest milestone not yet reached, or None when the event is finished."""
    for m in MILESTONES:
        if elapsed_seconds < m.seconds:
            return m
    return None
