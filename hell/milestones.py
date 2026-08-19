"""Milestone definitions and their reward announcements.

Milestones are evaluated against the **global event timer** only — individual
user time never influences them.
"""

from __future__ import annotations

from .models import Milestone

VERITIES_URL = "https://www.roblox.com/games/138268356635577/Find-the-Verities"

MILESTONES: tuple[Milestone, ...] = (
    Milestone(
        hours=32,
        title="🔥 32 HOURS SURVIVED",
        reward="@hell (limited)",
        blurb="Welcome to Hell has reached the first milestone.",
        short_reward="@hell",
    ),
    Milestone(
        hours=64,
        title="🔥🔥 64 HOURS — THE FIRE SPREADS",
        reward=f"Limited-time Verity in **Find the Verities** — {VERITIES_URL}",
        blurb="Two full days and change without the VC ever going quiet. The second milestone belongs to you.",
        short_reward="a limited Verity in Find the Verities",
    ),
    Milestone(
        hours=96,
        title="🔥🔥🔥 96 HOURS — HALFWAY IS BEHIND YOU",
        reward="@hell-ist (limited)",
        blurb="Four straight days in Hell. The third milestone is complete and there is no turning back now.",
        short_reward="@hell-ist",
    ),
    Milestone(
        hours=128,
        title="🔥🔥🔥🔥 128 HOURS — THE FINAL STRETCH",
        reward="Music permissions for everyone, provided they are not abused.",
        blurb="The fourth milestone has fallen. Only 32 hours stand between this VC and immortality.",
        short_reward="music permissions",
    ),
    Milestone(
        hours=160,
        title="🏆🔥 160 HOURS — WELCOME TO HELL COMPLETED",
        reward="@hell master (limited)",
        blurb="The full 160 consecutive hours have been survived. The final milestone is complete.",
        short_reward="@hell master",
    ),
)

FINAL_MILESTONE_HOURS: int = MILESTONES[-1].hours
TOTAL_SECONDS: float = float(FINAL_MILESTONE_HOURS * 3600)

TOP3_BONUS_ROLE = "@cool people :D"

_BY_HOURS = {m.hours: m for m in MILESTONES}


def get_milestone(hours: int) -> Milestone:
    return _BY_HOURS[hours]


def milestone_hours() -> tuple[int, ...]:
    return tuple(m.hours for m in MILESTONES)


def milestones_reached_at(elapsed_seconds: float) -> tuple[Milestone, ...]:
    """Every milestone whose threshold is covered by `elapsed_seconds`."""
    return tuple(m for m in MILESTONES if elapsed_seconds >= m.seconds)


def current_milestone(elapsed_seconds: float) -> Milestone | None:
    """Highest milestone already reached, or None."""
    reached = milestones_reached_at(elapsed_seconds)
    return reached[-1] if reached else None


def next_milestone(elapsed_seconds: float) -> Milestone | None:
    """Lowest milestone not yet reached, or None when the event is finished."""
    for m in MILESTONES:
        if elapsed_seconds < m.seconds:
            return m
    return None
