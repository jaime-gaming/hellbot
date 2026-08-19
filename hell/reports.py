"""Per-user end-of-event reports ("stat cards").

Pure logic: turns the frozen leaderboard plus the milestone snapshots into one
report per contestant, ready to be DM'd.  No Discord, no database.

The card answers the three things a participant cares about:

    WELCOME TO HELL

    0:00:00 SURVIVED          <- their own timeline #2 total (0 -> Xh)

    YOU WERE... TOP X         <- their final rank

    YOU WON X REWARDS         <- milestones they were present for
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .milestones import MILESTONES, TOP3_BONUS_ROLE, get_milestone
from .models import EventStatus, LeaderboardEntry, MilestoneRecord
from .timeutil import format_clock

OUTCOME_LINE = {
    EventStatus.COMPLETED: "The challenge was **COMPLETED** — 160 consecutive hours.",
    EventStatus.FAILED: "The challenge **FAILED** — the VC emptied before 160 hours.",
    EventStatus.CANCELLED: "The challenge was **CANCELLED** by a host.",
}


@dataclass
class UserReport:
    """Everything one contestant is told when the event ends."""

    user_id: int
    display_name: str
    seconds: float
    rank: int
    participants: int
    status: EventStatus
    event_elapsed: float
    milestones: list[int] = field(default_factory=list)   # hours they were present for
    rewards: list[str] = field(default_factory=list)      # human-readable reward list
    top3: bool = False
    bonus: bool = False                                    # earned @cool people :D

    @property
    def reward_count(self) -> int:
        return len(self.rewards)

    @property
    def survived(self) -> str:
        return format_clock(self.seconds)


def build_reports(
    entries: Sequence[LeaderboardEntry],
    milestone_records: Sequence[MilestoneRecord],
    *,
    status: EventStatus,
    event_elapsed: float,
) -> list[UserReport]:
    """One report per contestant on the (frozen) leaderboard."""
    present_at: dict[int, list[int]] = {}
    for record in milestone_records:
        for member in record.members:
            present_at.setdefault(member.user_id, []).append(record.hours)

    completed = status is EventStatus.COMPLETED
    reports: list[UserReport] = []
    for entry in entries:
        hours = sorted(present_at.get(entry.user_id, []))
        top3 = entry.rank <= 3
        bonus = completed and top3

        if bonus:
            # The final Top 3 receive every milestone reward plus the bonus role.
            rewards = [f"{m.hours}h — {m.reward}" for m in MILESTONES]
            rewards.append(f"Top 3 bonus — {TOP3_BONUS_ROLE}")
        else:
            rewards = [f"{h}h — {get_milestone(h).reward}" for h in hours]

        reports.append(
            UserReport(
                user_id=entry.user_id,
                display_name=entry.display_name,
                seconds=entry.seconds,
                rank=entry.rank,
                participants=len(entries),
                status=status,
                event_elapsed=event_elapsed,
                milestones=hours,
                rewards=rewards,
                top3=top3,
                bonus=bonus,
            )
        )
    return reports


def render_report(report: UserReport) -> str:
    """The DM body, in the requested shape."""
    lines = [
        "**WELCOME TO HELL**",
        "",
        f"**{report.survived} SURVIVED**",
        "",
        f"**YOU WERE... TOP {report.rank}**",
        f"*out of {report.participants} contestant(s)*",
        "",
        f"**YOU WON {report.reward_count} REWARD{'S' if report.reward_count != 1 else ''}**",
    ]
    if report.rewards:
        lines.extend(f"• {reward}" for reward in report.rewards)
        if report.bonus:
            lines.append("*Top 3: every milestone reward is yours.*")
        else:
            lines.append("*Claimable because you were in the VC when the milestone hit.*")
    else:
        lines.append("*You were not in the VC at any milestone moment — no rewards this time.*")

    lines += [
        "",
        OUTCOME_LINE.get(report.status, "The event has ended."),
        f"Event clock: **{format_clock(report.event_elapsed)}** of 160:00:00.",
    ]
    return "\n".join(lines)
