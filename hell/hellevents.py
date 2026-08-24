"""Hell Events subsystem — temporary randomized occurrences during Welcome to Hell.

Hell Events trigger only while the main event is RUNNING (never while IDLE,
FAILED, COMPLETED, CANCELLED, PAUSED, or during an empty-VC grace period).
Only one Hell Event may be active at any given time.

Event Types:
1. Double Time — 2x personal leaderboard time for humans in the VC.
2. Blood Pact — Instant +5m (or difficulty-scaled) bonus to everyone in the VC.
3. Inferno — Temporarily increased Alive/Dead check frequency.
4. Blindness — Temporarily hides remaining time & next milestone on progress cards.
5. Jackpot — Temporarily boosts gambling win multipliers.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import Config
from .models import ParticipantRef
from .storage import Store
from .texts import TEXT, say
from .timeutil import format_hm, now_ts

log = logging.getLogger("hell.hellevents")


class HellEventType(str, Enum):
    DOUBLE_TIME = "double_time"
    BLOOD_PACT = "blood_pact"
    INFERNO = "inferno"
    BLINDNESS = "blindness"
    JACKPOT = "jackpot"


class HellEventState(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class HellEventRecord:
    id: str
    event_type: HellEventType
    name: str
    start_ts: float
    end_ts: float
    state: HellEventState
    affected_users: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state is HellEventState.ACTIVE

    def seconds_left(self, now: float) -> float:
        return max(0.0, self.end_ts - now)

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type.value,
            "name": self.name,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "state": self.state.value,
            "affected_users": list(self.affected_users),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_row(cls, row: dict) -> HellEventRecord:
        return cls(
            id=row["id"],
            event_type=HellEventType(row["event_type"]),
            name=row["name"],
            start_ts=float(row["start_ts"]),
            end_ts=float(row["end_ts"]),
            state=HellEventState(row["state"]),
            affected_users=[int(u) for u in row.get("affected_users", [])],
            metadata=dict(row.get("metadata", {})),
        )


# ----------------------------------------------------------- event modifiers


@dataclass(frozen=True)
class HellEventModifier:
    event_type: HellEventType
    name: str
    duration_seconds: float
    time_multiplier: float = 1.0
    blood_pact_bonus_seconds: float = 300.0
    gamble_bonus_multiplier: float = 1.0
    inferno_min_check_seconds: float = 180.0
    inferno_max_check_seconds: float = 360.0


def get_event_modifier(event_type: HellEventType, difficulty_level: int = 0) -> HellEventModifier:
    lvl = max(0, min(4, difficulty_level))
    if event_type is HellEventType.DOUBLE_TIME:
        mult = 2.0 if lvl < 4 else 2.5
        return HellEventModifier(
            event_type=event_type,
            name="Double Time",
            duration_seconds=300.0,  # 5 minutes
            time_multiplier=mult,
        )
    elif event_type is HellEventType.BLOOD_PACT:
        bonus = 300.0 if lvl < 3 else (480.0 if lvl == 3 else 600.0)  # +5m, +8m, or +10m
        return HellEventModifier(
            event_type=event_type,
            name="Blood Pact",
            duration_seconds=0.0,  # Instant grant
            blood_pact_bonus_seconds=bonus,
        )
    elif event_type is HellEventType.INFERNO:
        return HellEventModifier(
            event_type=event_type,
            name="Inferno",
            duration_seconds=600.0,  # 10 minutes
            inferno_min_check_seconds=180.0,  # 3 minutes
            inferno_max_check_seconds=360.0,  # 6 minutes
        )
    elif event_type is HellEventType.BLINDNESS:
        return HellEventModifier(
            event_type=event_type,
            name="Blindness",
            duration_seconds=600.0,  # 10 minutes
        )
    elif event_type is HellEventType.JACKPOT:
        bonus_mult = 1.0 if lvl < 4 else 1.5
        return HellEventModifier(
            event_type=event_type,
            name="Hell Jackpot",
            duration_seconds=300.0,  # 5 minutes
            gamble_bonus_multiplier=bonus_mult,
        )
    raise ValueError(f"Unknown hell event type: {event_type}")


# ----------------------------------------------------------- domain events


@dataclass(frozen=True)
class HellEventStarted:
    record: HellEventRecord
    announcement_text: str
    eligible_participants: tuple[ParticipantRef, ...] = ()


@dataclass(frozen=True)
class HellEventEnded:
    record: HellEventRecord
    announcement_text: str


# ----------------------------------------------------------- manager


class HellEventManager:
    """Schedules, triggers, and manages temporary Hell Events."""

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        engine: Optional[Any] = None,
        announcer: Optional[Any] = None,
        alive_checks: Optional[Any] = None,
        rng: Optional[random.Random] = None,
    ):
        self.config = config
        self.store = store
        self.engine = engine
        self.announcer = announcer
        self.alive_checks = alive_checks
        self.rng = rng or random.Random()
        self.active_event: Optional[HellEventRecord] = None
        self._event_uid: Optional[str] = None

    def bind(self, event_uid: Optional[str], *, now: Optional[float] = None) -> None:
        """Bind to an event, restoring any active event or next schedule."""
        self._event_uid = event_uid
        self.active_event = None
        if not event_uid:
            return
        row = self.store.get_active_hell_event(event_uid)
        cur_now = now if now is not None else now_ts()
        if row:
            rec = HellEventRecord.from_row(row)
            if cur_now < rec.end_ts:
                self.active_event = rec
                log.info(
                    "Restored active Hell Event %s (%s) — %.0fs left",
                    rec.name,
                    rec.id,
                    rec.seconds_left(cur_now),
                )
            else:
                self.store.update_hell_event(rec.id, state=HellEventState.COMPLETED.value)
                log.info("Active Hell Event %s expired while offline", rec.name)
        if self.active_event is None and self.store.get_next_hell_event(event_uid) is None:
            self.schedule_next(cur_now)

    @property
    def bound_uid(self) -> Optional[str]:
        return self._event_uid

    def reset(self) -> None:
        self.active_event = None
        self._event_uid = None

    def shift_schedule(self, duration: float) -> None:
        """Shift active event end timestamp and next scheduled event timestamp forward by duration."""
        if not self._event_uid or duration <= 0:
            return
        if self.active_event is not None:
            self.active_event.end_ts += duration
            self.store.update_hell_event(self.active_event.id, end_ts=self.active_event.end_ts)
            log.info(
                "Shifted active Hell Event %s end_ts forward by %.1fs (new end_ts: %.1f)",
                self.active_event.name,
                duration,
                self.active_event.end_ts,
            )
        next_ts = self.store.get_next_hell_event(self._event_uid)
        if next_ts is not None:
            new_next = next_ts + duration
            self.store.set_next_hell_event(self._event_uid, new_next)
            log.info("Shifted next Hell Event forward by %.1fs (new next: %.1f)", duration, new_next)

    # ------------------------------------------------------------ scheduling

    def pick_next_interval(self, now: Optional[float] = None) -> float:
        """A random delay (30m–3h) scaled by difficulty."""
        diff_level = 0
        if self.engine is not None and getattr(self.engine, "status", None) and self.engine.status.is_active:
            diff_level = self.engine.current_difficulty(now).level
        # Scaling intervals: Diff 0 (60-180m), Diff 1 (45-150m), Diff 2 (35-120m), Diff 3 (30-90m), Diff 4 (20-60m)
        ranges = {
            0: (3600.0, 10800.0),
            1: (2700.0, 9000.0),
            2: (2100.0, 7200.0),
            3: (1800.0, 5400.0),
            4: (1200.0, 3600.0),
        }
        low, high = ranges.get(diff_level, (1800.0, 10800.0))
        return self.rng.uniform(low, high)

    def schedule_next(self, now: float) -> float:
        delay = self.pick_next_interval(now)
        when = now + delay
        if self._event_uid:
            self.store.set_next_hell_event(self._event_uid, when)
        log.info("Next Hell Event in %s (at %.0f)", format_hm(delay), when)
        return when

    def next_event_ts(self) -> Optional[float]:
        if not self._event_uid:
            return None
        return self.store.get_next_hell_event(self._event_uid)

    def is_due(self, now: float) -> bool:
        if self.active_event is not None or not self._event_uid:
            return False
        if self.engine is not None:
            if not self.engine.is_running or self.engine.is_paused or self.engine.grace.is_open:
                return False
        due = self.next_event_ts()
        if due is None:
            self.schedule_next(now)
            return False
        return now >= due

    # ------------------------------------------------------------------ tick

    async def tick(
        self, now: float, participants: Sequence[ParticipantRef]
    ) -> list[HellEventStarted | HellEventEnded]:
        """Tick Hell Events manager once per second."""
        if not self._event_uid:
            return []

        outcomes: list[HellEventStarted | HellEventEnded] = []

        # 1) Check active event expiration
        if self.active_event is not None:
            if now >= self.active_event.end_ts:
                ended = await self.end_active_event(now)
                if ended is not None:
                    outcomes.append(ended)
            return outcomes

        # 2) Check if a new event is due
        if self.is_due(now) and participants:
            ev_type = self.rng.choice(list(HellEventType))
            started = await self.start_event(ev_type, now, participants)
            if started is not None:
                outcomes.append(started)

        return outcomes

    # ------------------------------------------------------------- start event

    async def start_event(
        self,
        event_type: HellEventType,
        now: float,
        participants: Sequence[ParticipantRef],
        *,
        custom_duration: Optional[float] = None,
        custom_meta: Optional[dict] = None,
    ) -> Optional[HellEventStarted]:
        if not self._event_uid:
            return None
        if self.active_event is not None:
            log.warning("Cannot start Hell Event %s: an event is already active", event_type.value)
            return None

        diff_level = 0
        if self.engine is not None:
            diff_level = self.engine.current_difficulty(now).level

        mod = get_event_modifier(event_type, diff_level)
        duration = custom_duration if custom_duration is not None else mod.duration_seconds
        event_id = uuid.uuid4().hex
        meta = dict(custom_meta or {})

        affected: list[int] = []
        state = HellEventState.ACTIVE
        end_ts = now + duration

        # Event-specific logic
        if event_type is HellEventType.DOUBLE_TIME:
            meta["multiplier"] = mod.time_multiplier
            meta["duration_minutes"] = int(duration // 60)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_DOUBLE_TIME_START",
                    "🔥 **HELL EVENT — DOUBLE TIME**\n\nFor the next **{duration} minutes**, your personal leaderboard time is being multiplied by **{multiplier}x**.",
                ),
                duration=int(duration // 60),
                multiplier=f"{mod.time_multiplier:g}",
            )
        elif event_type is HellEventType.BLOOD_PACT:
            state = HellEventState.COMPLETED
            end_ts = now
            bonus = mod.blood_pact_bonus_seconds
            meta["bonus_seconds"] = bonus
            meta["bonus_minutes"] = int(bonus // 60)
            affected = [p.user_id for p in participants]
            if self.engine is not None:
                for p in participants:
                    self.engine.add_user_bonus_seconds(p.user_id, p.display_name, bonus, now)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_BLOOD_PACT_START",
                    "🩸 **HELL EVENT — BLOOD PACT**\n\nEveryone currently in Hell ({count} participants) has been granted **+{bonus}** of personal survival time.",
                ),
                count=len(participants),
                bonus=format_hm(bonus),
            )
        elif event_type is HellEventType.INFERNO:
            meta["duration_minutes"] = int(duration // 60)
            meta["min_check_seconds"] = mod.inferno_min_check_seconds
            meta["max_check_seconds"] = mod.inferno_max_check_seconds
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_INFERNO_START",
                    "🔥 **HELL EVENT — INFERNO**\n\nHell is getting hotter.\nAlive and Dead Checks will occur more frequently for the next **{duration} minutes**.",
                ),
                duration=int(duration // 60),
            )
        elif event_type is HellEventType.BLINDNESS:
            meta["duration_minutes"] = int(duration // 60)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_BLINDNESS_START",
                    "👁️ **HELL EVENT — BLINDNESS**\n\nFor the next **{duration} minutes**, Hell will hide your remaining time.",
                ),
                duration=int(duration // 60),
            )
        elif event_type is HellEventType.JACKPOT:
            meta["duration_minutes"] = int(duration // 60)
            meta["bonus_multiplier"] = mod.gamble_bonus_multiplier
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_JACKPOT_START",
                    "🎰 **HELL EVENT — JACKPOT**\n\nFor the next **{duration} minutes**, gambling rewards are increased.",
                ),
                duration=int(duration // 60),
            )
        else:
            ann_text = f"⚡ Hell Event {mod.name} has started."

        record = HellEventRecord(
            id=event_id,
            event_type=event_type,
            name=mod.name,
            start_ts=now,
            end_ts=end_ts,
            state=state,
            affected_users=affected,
            metadata=meta,
        )

        self.store.save_hell_event(
            self._event_uid,
            event_id=record.id,
            event_type=record.event_type.value,
            name=record.name,
            start_ts=record.start_ts,
            end_ts=record.end_ts,
            state=record.state.value,
            affected_users=record.affected_users,
            metadata=record.metadata,
        )

        if record.state is HellEventState.ACTIVE:
            self.active_event = record
        else:
            self.active_event = None
            self.schedule_next(now)

        log.info("Hell Event %s (%s) triggered (state %s)", record.name, record.id, record.state.value)

        event_out = HellEventStarted(
            record=record,
            announcement_text=ann_text,
            eligible_participants=tuple(participants),
        )

        # Broadcast announcement
        if self.announcer is not None:
            try:
                await self.announcer.announce_hell_event_start(event_out)
            except Exception:
                log.exception("Failed to announce Hell Event start")

        return event_out

    # ------------------------------------------------------------- end event

    async def end_active_event(self, now: float) -> Optional[HellEventEnded]:
        record = self.active_event
        if record is None or not self._event_uid:
            return None

        record.state = HellEventState.COMPLETED
        self.store.update_hell_event(record.id, state=HellEventState.COMPLETED.value)
        self.active_event = None
        self.schedule_next(now)

        if record.event_type is HellEventType.DOUBLE_TIME:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_DOUBLE_TIME_END",
                    "🔥 **DOUBLE TIME HAS ENDED**\n\nHell is no longer feeling generous.",
                )
            )
        elif record.event_type is HellEventType.INFERNO:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_INFERNO_END",
                    "🔥 **INFERNO HAS SUBSIDED**\n\nThe heat recedes. Check frequency has returned to normal.",
                )
            )
        elif record.event_type is HellEventType.BLINDNESS:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_BLINDNESS_END",
                    "👁️ **BLINDNESS HAS ENDED**\n\nYou can see the remaining time again.",
                )
            )
        elif record.event_type is HellEventType.JACKPOT:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_JACKPOT_END",
                    "🎰 **JACKPOT HAS ENDED**\n\nGambling rewards have returned to normal.",
                )
            )
        else:
            ann_text = f"⚡ Hell Event {record.name} has ended."

        log.info("Hell Event %s (%s) ended", record.name, record.id)

        event_out = HellEventEnded(record=record, announcement_text=ann_text)

        if self.announcer is not None:
            try:
                await self.announcer.announce_hell_event_end(event_out)
            except Exception:
                log.exception("Failed to announce Hell Event end")

        return event_out

    async def cancel_active(self, now: float, reason: str = "") -> None:
        """Cancel the active Hell Event without running end-state benefits."""
        if self.active_event is not None:
            rec = self.active_event
            rec.state = HellEventState.CANCELLED
            self.store.update_hell_event(rec.id, state=HellEventState.CANCELLED.value)
            self.active_event = None
            log.info("Cancelled active Hell Event %s (%s): %s", rec.name, rec.id, reason)

    # ------------------------------------------------------------- queries

    def get_time_multiplier(self, now: Optional[float] = None) -> float:
        """Return personal leaderboard time multiplier (e.g. 2.0x during Double Time)."""
        if self.active_event is not None and self.active_event.event_type is HellEventType.DOUBLE_TIME:
            cur_now = now if now is not None else now_ts()
            if cur_now < self.active_event.end_ts:
                return float(self.active_event.metadata.get("multiplier", 2.0))
        return 1.0

    def is_blindness_active(self, now: Optional[float] = None) -> bool:
        """Check if Blindness is currently hiding progress info."""
        if self.active_event is not None and self.active_event.event_type is HellEventType.BLINDNESS:
            cur_now = now if now is not None else now_ts()
            return cur_now < self.active_event.end_ts
        return False

    def is_inferno_active(self, now: Optional[float] = None) -> bool:
        """Check if Inferno is active."""
        if self.active_event is not None and self.active_event.event_type is HellEventType.INFERNO:
            cur_now = now if now is not None else now_ts()
            return cur_now < self.active_event.end_ts
        return False

    def get_gamble_modifier(self, now: Optional[float] = None) -> float:
        """Return bonus multiplier for gambling if Jackpot is active."""
        if self.active_event is not None and self.active_event.event_type is HellEventType.JACKPOT:
            cur_now = now if now is not None else now_ts()
            if cur_now < self.active_event.end_ts:
                return float(self.active_event.metadata.get("bonus_multiplier", 1.0))
        return 0.0

    def history(self) -> list[dict]:
        if not self._event_uid:
            return []
        return self.store.get_hell_events_history(self._event_uid)
