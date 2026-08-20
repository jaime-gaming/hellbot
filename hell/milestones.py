"""Milestone table — structure only; all wording lives in `Announcements.py`.

Milestones are evaluated against the **global event timer** (0 → 160h) only;
individual user time never influences them.
"""

from __future__ import annotations

import logging

from typing import Optional

from .models import Milestone
from .texts import TEXT

log = logging.getLogger("hell.milestones")

# Rebuilt in place by `refresh()` so that `from .milestones import MILESTONES`
# keeps working after a hot reload of Announcements.py.
MILESTONES: list[Milestone] = []
_BY_HOURS: dict[int, Milestone] = {}

TOTAL_SECONDS: float = 160 * 3600.0
FINAL_MILESTONE_HOURS: int = 160


_duration_locked = False


def _build() -> list[Milestone]:
    """Parse the milestone table, with errors a human can act on.

    Anything raised here is caught by `hell.texts.reload()`, which keeps the
    previously loaded table — a bad edit can never break a running event.
    """
    entries = list(TEXT.MILESTONES)
    if not entries:
        raise ValueError("MILESTONES in Announcements.py is empty — at least one is required")

    out: list[Milestone] = []
    seen: set[int] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"milestone #{index} in Announcements.py must be a {{...}} block")
        try:
            hours = int(entry["hours"])
        except KeyError:
            raise ValueError(f"milestone #{index} in Announcements.py has no 'hours'") from None
        except (TypeError, ValueError):
            raise ValueError(
                f"milestone #{index} in Announcements.py has a non-numeric 'hours': "
                f"{entry.get('hours')!r}"
            ) from None
        if hours <= 0:
            raise ValueError(f"milestone #{index} in Announcements.py must have hours > 0")
        if hours in seen:
            raise ValueError(f"Announcements.py lists {hours}h twice")
        seen.add(hours)
        for key in ("title", "blurb", "reward"):
            if not str(entry.get(key, "")).strip():
                raise ValueError(f"milestone {hours}h in Announcements.py is missing '{key}'")
        out.append(
            Milestone(
                hours=hours,
                title=str(entry["title"]),
                reward=str(entry["reward"]),
                blurb=str(entry["blurb"]),
                short_reward=str(entry.get("short_reward", "")),
                flavour=str(entry.get("flavour", "")),
            )
        )
    return sorted(out, key=lambda m: m.hours)


def refresh() -> list[Milestone]:
    """Rebuild the table from `Announcements.py` (called after a hot reload).

    The **event length never changes at runtime**: if an edit moves the final
    milestone, the new duration is refused and reported, because shortening or
    extending the clock mid-run would corrupt an event in progress.  Restart
    the bot to apply a duration change.
    """
    global TOTAL_SECONDS, FINAL_MILESTONE_HOURS, _duration_locked
    rebuilt = _build()
    final_hours = rebuilt[-1].hours

    if _duration_locked and final_hours != FINAL_MILESTONE_HOURS:
        log.error(
            "Announcements.py moves the last milestone from %sh to %sh. The running event keeps "
            "%sh — restart the bot to change the event length.",
            FINAL_MILESTONE_HOURS,
            final_hours,
            FINAL_MILESTONE_HOURS,
        )
    else:
        FINAL_MILESTONE_HOURS = final_hours
        TOTAL_SECONDS = float(final_hours * 3600)
        _duration_locked = True

    MILESTONES[:] = rebuilt
    _BY_HOURS.clear()
    _BY_HOURS.update({m.hours: m for m in rebuilt})
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
