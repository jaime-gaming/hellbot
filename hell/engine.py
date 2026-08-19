"""Event engine — state machine, user time tracking, milestone detection.

This module is intentionally free of any Discord import: it consumes plain
*observations* of the voice channel (already filtered: no bots, no `@clanker`)
and emits plain *domain events* that the Discord layer turns into messages.
That separation is what makes the whole thing unit-testable without a gateway
connection.

Timing rules implemented here
-----------------------------
* Elapsed time is always ``min(now, start + 160h) - start`` using absolute
  timestamps, so a restart cannot reset or shift the timer.
* The event never counts past 160 hours.
* Leaderboard credit is only granted for observed seconds while the event is
  RUNNING, capped per tick so downtime cannot be silently credited.
* Milestones are claimed atomically in SQLite, therefore each one triggers at
  most once, even across restarts.
* An empty VC (zero valid humans) fails the event immediately and permanently.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .config import Config
from .leaderboard import build_leaderboard, top_participants
from .milestones import (
    FINAL_MILESTONE_HOURS,
    MILESTONES,
    TOTAL_SECONDS,
    current_milestone,
    next_milestone,
)
from .models import EventState, EventStatus, LeaderboardEntry, Milestone, MilestoneRecord, ParticipantRef
from .storage import Store
from .timeutil import now_ts

log = logging.getLogger("hell.engine")


# --------------------------------------------------------------------- input


@dataclass(frozen=True)
class Observation:
    """One trusted look at the target voice channel."""

    now: float
    participants: tuple[ParticipantRef, ...] = ()

    @property
    def count(self) -> int:
        return len(self.participants)


# ------------------------------------------------------------ domain events


@dataclass
class DomainEvent:
    """Base class for everything the Discord layer may need to announce."""


@dataclass
class MilestoneReached(DomainEvent):
    milestone: Milestone
    reached_ts: float
    members: list[ParticipantRef]
    late: bool = False


@dataclass
class EventFailed(DomainEvent):
    failed_ts: float
    elapsed: float
    reason: str = "The voice channel became completely empty of valid participants."
    leaderboard: list[LeaderboardEntry] = field(default_factory=list)


@dataclass
class EventCompleted(DomainEvent):
    completed_ts: float
    leaderboard: list[LeaderboardEntry] = field(default_factory=list)
    top3: list[ParticipantRef] = field(default_factory=list)


@dataclass
class EventCancelled(DomainEvent):
    cancelled_ts: float
    elapsed: float
    by_user_id: Optional[int] = None
    leaderboard: list[LeaderboardEntry] = field(default_factory=list)


# ------------------------------------------------------------------ snapshot


@dataclass
class Snapshot:
    """Everything the progress / status renderers need."""

    status: EventStatus
    elapsed: float
    total: float
    remaining: float
    fraction: float
    participants: int
    current: Optional[Milestone]
    upcoming: Optional[Milestone]
    time_to_next: Optional[float]
    start_ts: Optional[float]
    end_ts: Optional[float]
    unverified: float = 0.0
    end_reason: Optional[str] = None


class StartError(RuntimeError):
    """Raised when /hell start cannot proceed."""


class HellEngine:
    """Owns the event state machine and all persistence interactions."""

    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self.state: EventState = store.load_state()
        self._last_participants: tuple[ParticipantRef, ...] = tuple(
            store.get_presence(self.state.event_uid) if self.state.event_uid else []
        )
        self._presence_signature: frozenset[int] = frozenset(
            p.user_id for p in self._last_participants
        )

    # ------------------------------------------------------------- accessors

    @property
    def status(self) -> EventStatus:
        return self.state.status

    @property
    def is_running(self) -> bool:
        return self.state.status is EventStatus.RUNNING

    @property
    def event_uid(self) -> Optional[str]:
        return self.state.event_uid

    @property
    def last_participants(self) -> tuple[ParticipantRef, ...]:
        return self._last_participants

    def elapsed(self, now: Optional[float] = None) -> float:
        """Elapsed event time, clamped to [0, 160h] and frozen once terminal."""
        if self.state.start_ts is None:
            return 0.0
        now = now_ts() if now is None else now
        reference = now
        if self.state.status.is_terminal and self.state.end_ts is not None:
            reference = self.state.end_ts
        return max(0.0, min(reference, self.state.start_ts + TOTAL_SECONDS) - self.state.start_ts)

    def snapshot(self, now: Optional[float] = None, participants: Optional[int] = None) -> Snapshot:
        now = now_ts() if now is None else now
        elapsed = self.elapsed(now)
        upcoming = next_milestone(elapsed)
        return Snapshot(
            status=self.state.status,
            elapsed=elapsed,
            total=TOTAL_SECONDS,
            remaining=max(0.0, TOTAL_SECONDS - elapsed),
            fraction=(elapsed / TOTAL_SECONDS) if TOTAL_SECONDS else 0.0,
            participants=len(self._last_participants) if participants is None else participants,
            current=current_milestone(elapsed),
            upcoming=upcoming,
            time_to_next=(upcoming.seconds - elapsed) if upcoming else None,
            start_ts=self.state.start_ts,
            end_ts=self.state.end_ts,
            unverified=self.store.get_unverified_seconds(self.state.event_uid) if self.state.event_uid else 0.0,
            end_reason=self.state.end_reason,
        )

    # --------------------------------------------------------------- control

    def start(
        self,
        *,
        now: Optional[float] = None,
        guild_id: int,
        voice_channel_id: int,
        announce_channel_id: int,
        started_by: int,
        initial_participants: Sequence[ParticipantRef] = (),
    ) -> EventState:
        """Begin a brand new event run.  Rejects if one is already RUNNING."""
        if self.is_running:
            raise StartError("An event is already RUNNING.")

        now = now_ts() if now is None else now
        uid = uuid.uuid4().hex
        self.state = EventState(
            status=EventStatus.RUNNING,
            event_uid=uid,
            start_ts=now,
            end_ts=None,
            last_tick_ts=now,
            guild_id=guild_id,
            voice_channel_id=voice_channel_id,
            announce_channel_id=announce_channel_id,
            progress_channel_id=None,
            progress_message_id=None,
            started_by=started_by,
            end_reason=None,
            final_saved=False,
        )
        self.store.save_state(self.state)
        participants = tuple(initial_participants)
        self._last_participants = participants
        self._presence_signature = frozenset(p.user_id for p in participants)
        self.store.replace_presence(uid, participants, now)
        self.store.touch_users(uid, participants, now)
        log.info("Event %s started at %.3f by %s", uid, now, started_by)
        return self.state

    def cancel(self, *, now: Optional[float] = None, by_user_id: Optional[int] = None) -> EventCancelled:
        """Manual stop by a host — CANCELLED, explicitly not FAILED."""
        if not self.is_running:
            raise StartError("No event is currently running.")
        now = now_ts() if now is None else now
        elapsed = self.elapsed(now)
        self._terminate(EventStatus.CANCELLED, now, "Manually stopped by a @gamenight host.")
        return EventCancelled(
            cancelled_ts=now,
            elapsed=elapsed,
            by_user_id=by_user_id,
            leaderboard=self.leaderboard(),
        )

    def reset(self) -> None:
        """Wipe everything for a completely new event."""
        self.store.reset_all()
        self.state = self.store.load_state()
        self._last_participants = ()
        self._presence_signature = frozenset()
        log.warning("Event data reset")

    def set_progress_message(self, channel_id: Optional[int], message_id: Optional[int]) -> None:
        self.state.progress_channel_id = channel_id
        self.state.progress_message_id = message_id
        self.store.set_progress_message(channel_id, message_id)

    # ------------------------------------------------------------------ tick

    def tick(self, obs: Observation) -> list[DomainEvent]:
        """Process one trusted VC observation.  Returns events to announce.

        The caller must only pass observations it trusts (guild cached, channel
        resolved, startup grace elapsed) — an untrusted observation could
        wrongly fail the event.
        """
        if not self.is_running or self.state.start_ts is None or self.state.event_uid is None:
            return []

        uid = self.state.event_uid
        start = self.state.start_ts
        deadline = start + TOTAL_SECONDS
        effective_now = min(obs.now, deadline)
        elapsed = max(0.0, effective_now - start)
        finished = obs.now >= deadline

        events: list[DomainEvent] = []
        self._last_participants = tuple(obs.participants)

        # 1) Leaderboard accrual for the observed interval.
        previous = self.state.last_tick_ts if self.state.last_tick_ts is not None else start
        raw_delta = max(0.0, effective_now - previous)
        credit = min(raw_delta, self.config.max_tick_credit)
        unverified = raw_delta - credit
        if unverified > 0.5:
            total = self.store.add_unverified_seconds(uid, unverified)
            log.warning(
                "Observation gap of %.1fs (bot downtime?) — not credited; total unverified %.1fs",
                unverified,
                total,
            )
        if credit > 0 and obs.participants:
            self.store.add_user_time(
                uid,
                [(p.user_id, p.display_name, credit, obs.now) for p in obs.participants],
            )
        elif obs.participants:
            self.store.touch_users(uid, obs.participants, obs.now)

        self.state.last_tick_ts = effective_now
        self.store.set_last_tick(effective_now)

        # Persist the participant list only when it actually changes: over a
        # 160h run that is ~576k avoided writes.
        signature = frozenset(p.user_id for p in obs.participants)
        if signature != self._presence_signature:
            self._presence_signature = signature
            self.store.replace_presence(uid, obs.participants, obs.now)

        # 2) Failure check — an empty VC before the deadline ends the run.
        #    (Once the 160h deadline is reached the run is already won, so
        #    completion takes precedence over an empty channel.)
        if not finished and obs.count == 0:
            self._terminate(
                EventStatus.FAILED,
                obs.now,
                "The voice channel became completely empty of valid participants.",
            )
            log.warning("Event %s FAILED at %.3f (elapsed %.1fs)", uid, obs.now, elapsed)
            return [
                EventFailed(
                    failed_ts=obs.now,
                    elapsed=elapsed,
                    leaderboard=self.leaderboard(),
                )
            ]

        # 3) Milestone detection (global timer only, each one exactly once).
        events.extend(self._trigger_due_milestones(elapsed, obs))

        # 4) Completion at exactly 160 hours — never counts beyond that.
        if finished:
            self._terminate(EventStatus.COMPLETED, deadline, "160 consecutive hours survived.")
            board = self.leaderboard()
            log.info("Event %s COMPLETED", uid)
            events.append(
                EventCompleted(
                    completed_ts=deadline,
                    leaderboard=board,
                    top3=top_participants(board, 3),
                )
            )
        return events

    # -------------------------------------------------------------- internals

    def _trigger_due_milestones(self, elapsed: float, obs: Observation) -> list[MilestoneReached]:
        assert self.state.event_uid is not None and self.state.start_ts is not None
        uid = self.state.event_uid
        out: list[MilestoneReached] = []
        already = self.store.triggered_milestone_hours(uid)
        for milestone in MILESTONES:
            if milestone.hours in already or elapsed < milestone.seconds:
                continue
            exact_ts = self.state.start_ts + milestone.seconds
            late = (obs.now - exact_ts) > max(3 * self.config.monitor_interval, 5.0)
            # Atomic claim: only the winner announces, so a restart in the same
            # second can never produce a duplicate message.
            if not self.store.claim_milestone(uid, milestone.hours, exact_ts, late=late):
                continue
            members = list(obs.participants)
            self.store.save_milestone_members(uid, milestone.hours, members)
            log.info("Milestone %sh reached (late=%s) with %d member(s)", milestone.hours, late, len(members))
            out.append(
                MilestoneReached(
                    milestone=milestone,
                    reached_ts=exact_ts,
                    members=members,
                    late=late,
                )
            )
        return out

    def _terminate(self, status: EventStatus, ts: float, reason: str) -> None:
        self.state.status = status
        self.state.end_ts = ts
        self.state.end_reason = reason
        self.store.save_state(self.state)
        self.freeze_leaderboard()

    # ----------------------------------------------------------- leaderboard

    def leaderboard(self) -> list[LeaderboardEntry]:
        """Live ranking, or the frozen final ranking once the event ended."""
        uid = self.state.event_uid
        if uid is None:
            return []
        if self.state.status.is_terminal and self.state.final_saved:
            saved = self.store.get_final_leaderboard(uid)
            if saved:
                return saved
        return build_leaderboard(self.store.get_user_times(uid))

    def freeze_leaderboard(self) -> list[LeaderboardEntry]:
        """Persist the final rankings so they can never drift afterwards."""
        uid = self.state.event_uid
        if uid is None:
            return []
        entries = build_leaderboard(self.store.get_user_times(uid))
        self.store.save_final_leaderboard(uid, entries)
        self.state.final_saved = True
        self.store.save_state(self.state)
        return entries

    # ------------------------------------------------------------ milestones

    def milestone_records(self) -> list[MilestoneRecord]:
        uid = self.state.event_uid
        return self.store.get_milestones(uid) if uid else []

    def pending_announcements(self) -> list[MilestoneRecord]:
        uid = self.state.event_uid
        return self.store.pending_announcements(uid) if uid else []

    def mark_announced(self, hours: int) -> None:
        if self.state.event_uid:
            self.store.mark_milestone_announced(self.state.event_uid, hours)

    @property
    def final_milestone_hours(self) -> int:
        return FINAL_MILESTONE_HOURS
