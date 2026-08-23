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

from collections.abc import Sequence
from dataclasses import dataclass, field

from .milestones import MILESTONES, get_milestone
from .models import EventStatus, LeaderboardEntry, MilestoneRecord
from .texts import TEXT, say
from .timeutil import format_clock


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
            rewards = [
                say(TEXT.CARD_MILESTONE_LINE, hours=m.hours, reward=m.reward) for m in MILESTONES
            ]
            rewards.append(say(TEXT.CARD_TOP3_BONUS_LINE, bonus_role=TEXT.TOP3_BONUS_ROLE))
        else:
            rewards = [
                say(TEXT.CARD_MILESTONE_LINE, hours=h, reward=get_milestone(h).reward)
                for h in hours
            ]

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
    """The DM body — its shape is defined by `CARD_BODY` in Announcements.py."""
    lines = [
        say(
            TEXT.CARD_BODY,
            survived=report.survived,
            rank=report.rank,
            participants=report.participants,
            reward_count=report.reward_count,
            reward_plural="S" if report.reward_count != 1 else "",
        )
    ]
    if report.rewards:
        lines.extend(say(TEXT.CARD_REWARD_LINE, reward=reward) for reward in report.rewards)
        lines.append(TEXT.CARD_REWARD_TOP3_NOTE if report.bonus else TEXT.CARD_REWARD_NOTE)
    else:
        lines.append(TEXT.CARD_NO_REWARDS)

    try:
        outcome = dict(TEXT.CARD_OUTCOME).get(report.status.value, TEXT.CARD_OUTCOME_DEFAULT)
    except (TypeError, ValueError):  # pragma: no cover - human-editable text file
        outcome = TEXT.CARD_OUTCOME_DEFAULT
    lines += [
        "",
        outcome,
        say(TEXT.CARD_EVENT_CLOCK, event_clock=format_clock(report.event_elapsed)),
    ]
    return "\n".join(lines)
