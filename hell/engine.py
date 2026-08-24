"""Event engine — state machine, user time tracking, milestone detection.

This module is intentionally free of any Discord import: it consumes plain
*observations* of the voice channel (already filtered: no bots, no `@clanker`)
and emits plain *domain events* that the Discord layer turns into messages.
That separation is what makes the whole thing unit-testable without a gateway
connection.

Two independent timelines
-------------------------
* **Global event timeline — 0 → 160h** (:mod:`hell.timeline`): one clock for
  the whole run, driven only by absolute timestamps.  Milestones and completion
  are measured against it, and *no individual user* can move it: people joining,
  leaving, being disconnected or being kicked never touch it.
* **Per-user session timelines — 0 → Xh** (:mod:`hell.tracking`): one clock per
  human, accumulating only while they are actually in the VC.  Leaving pauses a
  user's own clock and nothing else; rejoining resumes it on top of the total.

The only thing that can stop the global clock early is the VC being empty of
valid humans for longer than the grace window (:mod:`hell.grace`).

Timing rules implemented here
-----------------------------
* Elapsed time is always ``min(now, start + 160h) - start`` using absolute
  timestamps, so a restart cannot reset or shift the timer.
* The event never counts past 160 hours.
* Leaderboard credit is only granted for observed seconds while the event is
  RUNNING, capped per tick so downtime cannot be silently credited.
* Milestones are claimed atomically in SQLite, therefore each one triggers at
  most once, even across restarts.
* An empty VC (zero valid humans) opens the grace window; only when that window
  expires does the event fail — permanently, timestamped at the moment the VC
  actually emptied.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .grace import ALIVE_CHECK_RECOVERY_GRACE_SECONDS, EmptyVcGracePeriod
from .leaderboard import top_participants
from .milestones import (
    FINAL_MILESTONE_HOURS,
    MILESTONES,
    TOTAL_SECONDS,
    current_milestone,
    next_milestone,
)
from .models import EventState, EventStatus, LeaderboardEntry, Milestone, MilestoneRecord, ParticipantRef
from .storage import Store
from .timeline import EventTimeline
from .timeutil import format_hm, now_ts
from .tracking import UserTimeTracker

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
class GraceStarted(DomainEvent):
    """The VC just emptied: the run dies unless somebody joins in time."""

    started_ts: float
    deadline_ts: float
    seconds: float
    elapsed: float


@dataclass
class GraceRecovered(DomainEvent):
    """Somebody joined before the grace window expired — the run continues."""

    started_ts: float
    recovered_ts: float
    empty_for: float
    participants: list[ParticipantRef] = field(default_factory=list)


@dataclass
class EventFailed(DomainEvent):
    failed_ts: float
    elapsed: float
    reason: str = "The voice channel stayed empty of valid participants for the whole grace period."
    empty_since: Optional[float] = None
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
    grace_open: bool = False
    grace_seconds_left: float = 0.0
    grace_total: float = 0.0
    paused: bool = False


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
        # Timeline #2 (per-user 0 -> Xh). Timeline #1 (global 0 -> 160h) is
        # built on demand from the start timestamp; see `self.timeline`.
        self.tracker = UserTimeTracker(store, max_credit=config.max_tick_credit)
        self.grace = EmptyVcGracePeriod(
            seconds=config.empty_vc_grace_seconds,
            default_seconds=config.empty_vc_grace_seconds,
        )
        self.grace.restore(self.state.grace_started_ts, seconds=self.state.grace_duration)
        self._alive_check_emptied_ts: Optional[float] = None

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
    def is_paused(self) -> bool:
        """The event is frozen: neither the global clock nor any user's
        clock advances, and nothing can fail or trigger."""
        return self.state.paused_ts is not None

    @property
    def last_participants(self) -> tuple[ParticipantRef, ...]:
        return self._last_participants

    @property
    def timeline(self) -> Optional[EventTimeline]:
        """The global 0 → 160h clock, or None before the event starts."""
        if self.state.start_ts is None:
            return None
        return EventTimeline(start_ts=self.state.start_ts, total=TOTAL_SECONDS)

    def _effective_now(self, now: float) -> float:
        """`now` with every pause the event has taken removed.

        The event clock is absolute-timestamp based, so a pause is expressed
        as a running offset: while paused, the offset keeps growing and the
        effective time stands still; after a resume the offset is frozen and
        the clock carries on exactly where it stopped.
        """
        paused = self.state.paused_seconds
        if self.state.paused_ts is not None:
            paused += max(0.0, now - self.state.paused_ts)
        return now - paused

    def elapsed(self, now: Optional[float] = None) -> float:
        """Elapsed event time, clamped to [0, 160h] and frozen once terminal.

        Pause time is never counted: while paused the value stands still, and
        after a resume it continues exactly where it stopped.
        """
        timeline = self.timeline
        if timeline is None:
            return 0.0
        now = now_ts() if now is None else now
        frozen = self.state.end_ts if (self.state.status.is_terminal and self.state.end_ts) else None
        return timeline.elapsed(self._effective_now(now), frozen_at=frozen)

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
            grace_open=self.grace.is_open and self.is_running and not self.is_paused,
            grace_seconds_left=self.grace.seconds_left(self._effective_now(now)),
            grace_total=self.grace.seconds,
            paused=self.is_paused,
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
        self.grace.restore(None)
        self.store.save_state(self.state)
        participants = tuple(initial_participants)
        self._last_participants = participants
        self._presence_signature = frozenset(p.user_id for p in participants)
        self.store.replace_presence(uid, participants, now)
        self.store.touch_users(uid, participants, now)
        log.info("Event %s started at %.3f by %s", uid, now, started_by)
        return self.state

    def pause(self, *, now: Optional[float] = None, reason: str = "") -> EventState:
        """Freeze the whole event: global clock, per-user clocks, milestones,
        grace countdown — nothing moves or can fail while paused.

        Used when something goes wrong and the operator needs time to fix it
        without the 160h run being punished.  Persisted, so a restart while
        paused stays paused.
        """
        if not self.is_running:
            raise StartError("Cannot pause — the event is not running.")
        if self.is_paused:
            raise StartError("The event is already paused.")
        now = now_ts() if now is None else now
        self.state.paused_ts = now
        self.state.pause_reason = reason or None
        self.store.save_state(self.state)
        log.warning(
            "Event %s PAUSED at %.3f%s", self.state.event_uid, now,
            f" ({reason})" if reason else "",
        )
        return self.state

    def resume(self, *, now: Optional[float] = None) -> EventState:
        """Unfreeze the event after a pause.

        The pause duration is banked so it is never counted against the 160h,
        and an open empty-VC grace window is shifted forward by the pause so
        its countdown resumes where it left off instead of expiring mid-pause.
        """
        if not self.is_running:
            raise StartError("Cannot resume — the event is not running.")
        if not self.is_paused:
            raise StartError("The event is not paused.")
        now = now_ts() if now is None else now
        duration = max(0.0, now - (self.state.paused_ts or now))
        self.state.paused_seconds += duration
        self.state.paused_ts = None
        self.state.pause_reason = None
        # Resume the clock exactly where it stopped (no bogus observation gap).
        self.state.last_tick_ts = self._effective_now(now)
        # The grace window was frozen during the pause: shift it forward.
        if self.state.grace_started_ts is not None:
            self.state.grace_started_ts += duration
            self.grace.restore(self.state.grace_started_ts)
        self.store.save_state(self.state)
        log.warning(
            "Event %s resumed after %.1fs paused (total paused %.1fs)",
            self.state.event_uid, duration, self.state.paused_seconds,
        )
        return self.state

    def notify_alive_check_emptied(self, now: Optional[float] = None) -> None:
        """Record that an Are You Alive? check caused the VC to become empty."""
        ts = now if now is not None else now_ts()
        self._alive_check_emptied_ts = ts
        if self.state.event_uid:
            self.store.set_alive_check_emptied(self.state.event_uid, ts)

    def resume_failed(self, *, now: Optional[float] = None) -> EventState:
        """Continue a run that previously reached FAILED status.

        The time during which the run was failed is banked into paused_seconds
        so elapsed time continues exactly where it failed.
        """
        if self.state.status is not EventStatus.FAILED:
            raise StartError("Cannot resume — the event has not failed.")
        now = now_ts() if now is None else now
        if self.state.end_ts is not None:
            self.state.paused_seconds = max(self.state.paused_seconds, now - self.state.end_ts)
        self.state.status = EventStatus.RUNNING
        self.state.end_ts = None
        self.state.end_reason = None
        self.state.final_saved = False
        self.grace.restore(None)
        self.state.grace_started_ts = None
        self.state.grace_duration = None
        self.state.paused_ts = None
        self.state.pause_reason = None
        self.state.last_tick_ts = self._effective_now(now)
        self.store.save_state(self.state)
        log.warning(
            "Event %s RESUMED from FAILED state (total paused %.1fs)",
            self.state.event_uid, self.state.paused_seconds,
        )
        return self.state

    def cancel(self, *, now: Optional[float] = None, by_user_id: Optional[int] = None) -> EventCancelled:
        """Manual stop by a host — CANCELLED, explicitly not FAILED."""
        if not self.is_running:
            raise StartError("No event is currently running.")
        now = now_ts() if now is None else now
        elapsed = self.elapsed(now)
        self._terminate(EventStatus.CANCELLED, self._effective_now(now),
                        "Manually stopped by a @gamenight host.")
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
        self.grace.restore(None)
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
        timeline = self.timeline
        assert timeline is not None
        effective_now = timeline.clamp(self._effective_now(obs.now))
        elapsed = self.elapsed(obs.now)
        finished = timeline.is_finished(effective_now)

        events: list[DomainEvent] = []
        self._last_participants = tuple(obs.participants)

        # While paused, the event is completely frozen: no credit, no grace
        # evaluation, no milestones, no completion.  Only the participant
        # list above stays fresh so `/hell status` shows reality.
        if self.is_paused:
            return []

        # 1) Timeline #2: advance each present user's own 0 -> Xh clock.
        #    (Step 2 — last-valid-observed — is inserted after credit, below.)
        #    Nothing here can affect the global 0 -> 160h timeline.
        previous = self.state.last_tick_ts if self.state.last_tick_ts is not None else timeline.start_ts
        bridge_users, bridge_seconds = self._bridge(previous, effective_now, obs)
        self.tracker.credit(
            uid,
            previous_ts=previous,
            now_ts=effective_now,
            participants=obs.participants,
            stamp=obs.now,
            bridge_users=bridge_users,
            bridge_seconds=bridge_seconds,
        )

        self.state.last_tick_ts = effective_now
        self.store.set_last_tick(effective_now)

        # Persist the participant list only when it actually changes: over a
        # 160h run that is ~576k avoided writes.
        signature = frozenset(p.user_id for p in obs.participants)
        if signature != self._presence_signature:
            self._presence_signature = signature
            self.store.replace_presence(uid, obs.participants, obs.now)

        # 2) Track last-valid-observed timestamp for crash recovery.
        #    This is the core reconstruction field: after a restart, the
        #    system can tell *when* the VC was last occupied, which is
        #    different from last_tick_ts (which is updated even on empty
        #    observations).
        if obs.count > 0:
            if self.state.last_valid_observed_ts != effective_now:
                self.state.last_valid_observed_ts = effective_now
                self.store.set_last_valid_observed_ts(effective_now)

        # 3) Empty-VC grace window (see hell/grace.py).
        if not finished:
            grace_event = self._evaluate_grace(obs, elapsed)
            if isinstance(grace_event, EventFailed):
                return [grace_event]
            if grace_event is not None:
                events.append(grace_event)

        # 4) Milestone detection (global timeline only, each one exactly once).
        #    Skipped while the VC is empty: nobody would be able to claim it,
        #    so the milestone waits for the first tick with people present.
        if obs.participants:
            events.extend(self._trigger_due_milestones(elapsed, obs))

        # 5) Completion at exactly 160 hours — never counts beyond that.
        #    `effective_now` is clamped to the deadline, so a completion that
        #    lands right after a long pause still records exactly 160h.
        if finished:
            self._terminate(EventStatus.COMPLETED, effective_now, "160 consecutive hours survived.")
            board = self.leaderboard()
            log.info("Event %s COMPLETED", uid)
            events.append(
                EventCompleted(
                    completed_ts=effective_now,
                    leaderboard=board,
                    top3=top_participants(board, 3),
                )
            )
        return events

    def _bridge(self, previous: float, now: float, obs: Observation) -> tuple[frozenset[int], float]:
        """Work out how much of an outage can be credited back.

        If the bot was away only briefly and somebody was in the VC both before
        it went down and now, they demonstrably never left — so their clock is
        made whole instead of losing the gap. Anyone who arrived during the
        outage gets nothing extra, because nothing observed them.
        """
        gap = max(0.0, now - previous)
        allowance = self.config.downtime_credit_seconds
        if gap <= self.config.max_tick_credit or gap > allowance:
            return frozenset(), 0.0

        present_now = {p.user_id for p in obs.participants}
        continuous = frozenset(self._presence_signature & present_now)
        if not continuous:
            log.warning(
                "Gap of %.0fs and nobody from before is still here — that time is not credited",
                gap,
            )
            return frozenset(), 0.0

        log.info(
            "Recovered from a %.0fs gap; %d user(s) were here before and after, crediting it back",
            gap,
            len(continuous),
        )
        return continuous, gap - self.config.max_tick_credit

    # ------------------------------------------------------------ grace rule

    def _evaluate_grace(self, obs: Observation, elapsed: float) -> Optional[DomainEvent]:
        """Apply the empty-VC grace rule for one observation.

        Returns a :class:`GraceStarted`, a :class:`GraceRecovered`, an
        :class:`EventFailed` (window expired), or None when nothing changed.
        """
        assert self.state.event_uid is not None

        if obs.count > 0:
            # Someone valid is in the VC: close any open window.
            started = self.grace.close()
            self._alive_check_emptied_ts = None
            if self.state.event_uid:
                self.store.clear_alive_check_emptied(self.state.event_uid)
            if started is None:
                return None
            self._persist_grace(None, None)
            empty_for = max(0.0, obs.now - started)
            log.warning(
                "VC repopulated after %.1fs empty — the run continues (%d person(s) back)",
                empty_for,
                obs.count,
            )
            return GraceRecovered(
                started_ts=started,
                recovered_ts=obs.now,
                empty_for=empty_for,
                participants=list(obs.participants),
            )

        # VC is empty of valid humans.
        if not self.grace.is_open:
            is_alive_check = False
            if self._alive_check_emptied_ts is not None and abs(obs.now - self._alive_check_emptied_ts) <= 60.0:
                is_alive_check = True
            elif self.state.event_uid:
                emptied_ts = self.store.get_alive_check_emptied(self.state.event_uid)
                if emptied_ts is not None and abs(obs.now - emptied_ts) <= 60.0:
                    is_alive_check = True
            self._alive_check_emptied_ts = None
            if self.state.event_uid:
                self.store.clear_alive_check_emptied(self.state.event_uid)

            grace_duration = ALIVE_CHECK_RECOVERY_GRACE_SECONDS if is_alive_check else self.config.empty_vc_grace_seconds
            started = self.grace.open(obs.now, seconds=grace_duration)
            self._persist_grace(started, grace_duration)
            deadline = self.grace.deadline() or obs.now
            log.warning(
                "VC is EMPTY — grace period of %.0fs started%s, failing at %.3f unless someone joins",
                self.grace.seconds,
                " (alive-check recovery)" if is_alive_check else "",
                deadline,
            )
            if not self.grace.has_expired(obs.now):
                return GraceStarted(
                    started_ts=started,
                    deadline_ts=deadline,
                    seconds=self.grace.seconds,
                    elapsed=elapsed,
                )

        if self.grace.has_expired(obs.now):
            # The run dies as of the moment the VC emptied, never later, so a
            # grace window can never inflate the survived time.
            empty_since = self.grace.empty_since or obs.now
            timeline = self.timeline
            failed_elapsed = self.elapsed(empty_since) if timeline else elapsed
            # CRASH-SAFE ORDERING: _terminate() — which writes status="FAILED"
            # to the DB and clears grace_started_ts — MUST run first, before we
            # touch the grace state in memory.  If the bot crashes halfway, the
            # DB event status is already FAILED, so a restart will not see it
            # as still RUNNING with a missing grace window.
            self._terminate(
                EventStatus.FAILED,
                self._effective_now(empty_since),
                "The voice channel stayed empty of valid participants for the whole "
                f"{self.grace.seconds:.0f}s grace period.",
            )
            # _terminate already cleared self.grace and persisted, so we skip
            # the redundant self.grace.close() and self._persist_grace(None)
            # that used to sit here (and would have created a crash window
            # between "grace cleared" and "status=FAILED written").
            log.warning(
                "Event %s FAILED — VC empty since %.3f, grace expired at %.3f (elapsed %.1fs)",
                self.state.event_uid,
                empty_since,
                obs.now,
                failed_elapsed,
            )
            return EventFailed(
                failed_ts=empty_since,
                elapsed=failed_elapsed,
                empty_since=empty_since,
                leaderboard=self.leaderboard(),
            )
        return None

    def _persist_grace(self, started: Optional[float], duration: Optional[float] = None) -> None:
        self.state.grace_started_ts = started
        self.state.grace_duration = duration if started is not None else None
        self.store.set_grace_started(started, self.state.grace_duration)

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

    def _terminate(self, status: EventStatus, ts_effective: float, reason: str) -> None:
        """End the event.  `ts_effective` is pause-adjusted event time — the
        value `elapsed()` will freeze at, so a cancelled/failed/completed run
        reports exactly the time it actually survived, never the pause.
        """
        self.state.status = status
        self.state.end_ts = ts_effective
        self.state.end_reason = reason
        # A half-open grace window must not survive the run it belonged to,
        # and an open pause is closed (its banked seconds stay in the clock).
        self.grace.restore(None)
        self.state.grace_started_ts = None
        self.state.grace_duration = None
        self.state.paused_ts = None
        self.state.pause_reason = None
        self.store.save_state(self.state)
        log.info(
            "Event %s: %s -> %s after %s (%s)",
            self.state.event_uid,
            self.state.status.value,
            status.value,
            format_hm(self.elapsed()),
            reason,
        )
        self.freeze_leaderboard()

    # ----------------------------------------------------------- leaderboard

    def leaderboard(self, uid: Optional[str] = None) -> list[LeaderboardEntry]:
        """Live ranking, or the frozen final ranking once the event ended.

        `uid` defaults to the current event.  Passing an explicit UID lets
        long-running jobs (end-of-event stat cards) pin the event they belong
        to, so a brand new event starting mid-delivery can never make them
        read the wrong leaderboard.
        """
        uid = uid or self.state.event_uid
        if uid is None:
            return []
        if self.state.status.is_terminal and self.state.final_saved:
            saved = self.store.get_final_leaderboard(uid)
            if saved:
                return saved
        return self.tracker.totals(uid)   # timeline #2: per-user 0 -> Xh

    def freeze_leaderboard(self) -> list[LeaderboardEntry]:
        """Persist the final rankings so they can never drift afterwards."""
        uid = self.state.event_uid
        if uid is None:
            return []
        entries = self.tracker.totals(uid)
        self.store.save_final_leaderboard(uid, entries)
        log.info(
            "Final leaderboard frozen: %d participant(s), %s tracked in total",
            len(entries),
            format_hm(sum(e.seconds for e in entries)),
        )
        self.state.final_saved = True
        self.store.save_state(self.state)
        return entries

    # ------------------------------------------------------------ milestones

    def milestone_records(self, uid: Optional[str] = None) -> list[MilestoneRecord]:
        uid = uid or self.state.event_uid
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
