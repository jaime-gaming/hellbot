"""Plain data models shared by every subsystem (no Discord imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EventStatus(str, Enum):
    """Lifecycle of a `Welcome to Hell` run."""

    IDLE = "IDLE"            # no event has been started yet
    RUNNING = "RUNNING"      # timer is counting
    FAILED = "FAILED"        # VC became empty of valid humans -> permanent stop
    COMPLETED = "COMPLETED"  # 160 consecutive hours reached
    CANCELLED = "CANCELLED"  # manually stopped by a @gamenight host

    @property
    def is_terminal(self) -> bool:
        return self in (EventStatus.FAILED, EventStatus.COMPLETED, EventStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self is EventStatus.RUNNING


@dataclass(frozen=True)
class Milestone:
    """A global-timer milestone (never based on individual user time)."""

    hours: int
    title: str
    reward: str
    blurb: str
    short_reward: str = ""
    flavour: str = ""
    role_token: str = ""     # text to replace with a real mention
    role_env: str = ""       # which .env role ID that mention uses

    @property
    def seconds(self) -> float:
        return self.hours * 3600.0


@dataclass
class MilestoneRecord:
    """A milestone that has already been reached and persisted."""

    hours: int
    reached_ts: float
    announced: bool = False
    late: bool = False  # reached while the bot was offline; snapshot taken on catch-up
    members: list[ParticipantRef] = field(default_factory=list)


@dataclass(frozen=True)
class ParticipantRef:
    """A user reference that survives the user leaving the guild."""

    user_id: int
    display_name: str

    def mention(self) -> str:
        return f"<@{self.user_id}>"


@dataclass
class LeaderboardEntry:
    rank: int
    user_id: int
    display_name: str
    seconds: float

    def mention(self) -> str:
        return f"<@{self.user_id}>"


@dataclass
class EventState:
    """The persisted state of the current (or last) event."""

    status: EventStatus = EventStatus.IDLE
    event_uid: Optional[str] = None
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None          # fail / complete / cancel timestamp
    last_tick_ts: Optional[float] = None    # last trusted VC observation
    guild_id: Optional[int] = None
    voice_channel_id: Optional[int] = None
    announce_channel_id: Optional[int] = None
    progress_channel_id: Optional[int] = None
    progress_message_id: Optional[int] = None
    started_by: Optional[int] = None
    end_reason: Optional[str] = None
    final_saved: bool = False
    grace_started_ts: Optional[float] = None   # empty-VC grace window in progress
