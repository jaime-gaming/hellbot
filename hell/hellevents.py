"""Hell Events subsystem — temporary randomized occurrences during Welcome to Hell.

Hell Events trigger only while the main event is RUNNING (never while IDLE,
FAILED, COMPLETED, CANCELLED, PAUSED, or during an empty-VC grace period).
Only one Hell Event may be active at any given time.

Event Types (good):
1. Double Time — 2x personal leaderboard time for humans in the VC.
2. Blood Pact — Instant +5m (or difficulty-scaled) bonus to everyone in the VC.
3. Hell Jackpot — Temporarily boosts gambling win multipliers.
4. Golden Hour — Instantly postpones the next roll call.
5. Soul Cache — Instant big bonus, but for ONE random person in the VC.

Event Types (bad):
6. Inferno — Temporarily increased Alive/Dead check frequency.
7. Blindness — Temporarily hides remaining time & next milestone on progress cards.
8. Time Vortex — Personal leaderboard time runs at half (or quarter) speed.
9. Blood Debt — Instant survival-time penalty for everyone in the VC.
10. The Culling — Triggers an immediate roll call: reply Yes or be disconnected.

Secret Events:
Roughly one in four randomly triggered events is SECRET: the announcement only
says that *something* happened — the event's name, duration and effect stay
hidden until the event ends and the veil is lifted.  Instant events can never
be secret (their effect is visible the moment they happen), so secrets are
drawn from :data:`TIMED_EVENT_TYPES` only.
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
    TIME_VORTEX = "time_vortex"
    GOLDEN_HOUR = "golden_hour"
    SOUL_CACHE = "soul_cache"
    BLOOD_DEBT = "blood_debt"
    CULLING = "culling"


# Chance that a randomly scheduled event is a SECRET event (announcement says
# something happened, but not what — revealed only when it ends).
SECRET_EVENT_CHANCE = 0.25

# Events with a real duration — the only candidates for SECRET events, because
# an instant event (Blood Pact, The Culling, …) is obvious the second it fires.
TIMED_EVENT_TYPES: tuple[HellEventType, ...] = (
    HellEventType.DOUBLE_TIME,
    HellEventType.INFERNO,
    HellEventType.BLINDNESS,
    HellEventType.JACKPOT,
    HellEventType.TIME_VORTEX,
)


def is_secret_record(record: HellEventRecord | dict) -> bool:
    """Was this event a secret?  Works for live records and stored rows."""
    if isinstance(record, dict):
        return bool(record.get("metadata", {}).get("secret"))
    return bool(record.metadata.get("secret"))


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
    soul_cache_bonus_seconds: float = 600.0
    blood_debt_penalty_seconds: float = 120.0
    golden_hour_postpone_seconds: float = 1800.0


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
    elif event_type is HellEventType.TIME_VORTEX:
        mult = 0.5 if lvl < 4 else 0.25  # half speed — a quarter on Cataclysm
        return HellEventModifier(
            event_type=event_type,
            name="Time Vortex",
            duration_seconds=300.0,  # 5 minutes
            time_multiplier=mult,
        )
    elif event_type is HellEventType.GOLDEN_HOUR:
        # The hotter Hell runs, the sweeter the relief: 30m … 50m of delay.
        postpone = 1800.0 + 300.0 * lvl
        return HellEventModifier(
            event_type=event_type,
            name="Golden Hour",
            duration_seconds=0.0,  # Instant effect
            golden_hour_postpone_seconds=postpone,
        )
    elif event_type is HellEventType.SOUL_CACHE:
        bonus = 600.0 + 150.0 * lvl  # +10m … +20m for one lucky soul
        return HellEventModifier(
            event_type=event_type,
            name="Soul Cache",
            duration_seconds=0.0,  # Instant grant
            soul_cache_bonus_seconds=bonus,
        )
    elif event_type is HellEventType.BLOOD_DEBT:
        penalty = 120.0 + 45.0 * lvl  # -2m … -5m for everyone in the VC
        return HellEventModifier(
            event_type=event_type,
            name="Blood Debt",
            duration_seconds=0.0,  # Instant penalty
            blood_debt_penalty_seconds=penalty,
        )
    elif event_type is HellEventType.CULLING:
        return HellEventModifier(
            event_type=event_type,
            name="The Culling",
            duration_seconds=0.0,  # Instant roll call
        )
    raise ValueError(f"Unknown hell event type: {event_type}")


# ----------------------------------------------------------- domain events


@dataclass(frozen=True)
class HellEventStarted:
    record: HellEventRecord
    announcement_text: str
    eligible_participants: tuple[ParticipantRef, ...] = ()
    secret: bool = False


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
            secret = self.rng.random() < SECRET_EVENT_CHANCE
            if secret:
                # Secrets need a duration to stay hidden — instant events give
                # themselves away the moment they happen.
                ev_type = self.rng.choice(TIMED_EVENT_TYPES)
            else:
                ev_type = self.rng.choice(list(HellEventType))
            started = await self.start_event(ev_type, now, participants, secret=secret)
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
        secret: bool = False,
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
        # An instant event is obvious the second it fires — it cannot be secret.
        if secret and duration <= 0:
            secret = False
        if secret:
            meta["secret"] = True

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
        elif event_type is HellEventType.TIME_VORTEX:
            meta["multiplier"] = mod.time_multiplier
            meta["duration_minutes"] = int(duration // 60)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_TIME_VORTEX_START",
                    "🌀 **HELL EVENT — TIME VORTEX**\n\nFor the next **{duration} minutes**, your personal leaderboard time is running at **{multiplier}x speed**.\nEvery second in Hell now counts for less.",
                ),
                duration=int(duration // 60),
                multiplier=f"{mod.time_multiplier:g}",
            )
        elif event_type is HellEventType.GOLDEN_HOUR:
            postpone_by = mod.golden_hour_postpone_seconds
            new_due = None
            if self.alive_checks is not None:
                new_due = self.alive_checks.postpone_next(postpone_by, now)
            if new_due is None:
                # A roll call is pending right now (or checks are off) — there is
                # nothing to postpone, so the event honestly does not happen.
                log.info("Golden Hour skipped: no roll call could be postponed")
                self.schedule_next(now)
                return None
            state = HellEventState.COMPLETED
            end_ts = now
            meta["postpone_seconds"] = postpone_by
            affected = [p.user_id for p in participants]
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_GOLDEN_HOUR_START",
                    "😇 **HELL EVENT — GOLDEN HOUR**\n\nHell looks away for a moment. Your next roll call has been postponed by **{delay}**.\nBreathe. You have earned it.",
                ),
                delay=format_hm(postpone_by),
            )
        elif event_type is HellEventType.SOUL_CACHE:
            if not participants:
                self.schedule_next(now)
                return None
            winner = self.rng.choice(list(participants))
            bonus = mod.soul_cache_bonus_seconds
            state = HellEventState.COMPLETED
            end_ts = now
            meta["winner_id"] = winner.user_id
            meta["bonus_seconds"] = bonus
            affected = [winner.user_id]
            if self.engine is not None:
                self.engine.add_user_bonus_seconds(winner.user_id, winner.display_name, bonus, now)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_SOUL_CACHE_START",
                    "💎 **HELL EVENT — SOUL CACHE**\n\nA hidden cache of stolen time has surfaced — and **{who}** found it first.\n**+{bonus}** of personal survival time, on the house.",
                ),
                who=winner.display_name,
                bonus=format_hm(bonus),
            )
        elif event_type is HellEventType.BLOOD_DEBT:
            penalty = mod.blood_debt_penalty_seconds
            state = HellEventState.COMPLETED
            end_ts = now
            meta["penalty_seconds"] = penalty
            affected = [p.user_id for p in participants]
            if self.engine is not None:
                for p in participants:
                    self.engine.add_user_bonus_seconds(p.user_id, p.display_name, -penalty, now)
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_BLOOD_DEBT_START",
                    "📉 **HELL EVENT — BLOOD DEBT**\n\nThe tax collectors of Hell have come knocking. Everyone currently in Hell ({count} participants) has been charged **-{penalty}** of personal survival time.",
                ),
                count=len(participants),
                penalty=format_hm(penalty),
            )
        elif event_type is HellEventType.CULLING:
            check = None
            if self.alive_checks is not None:
                check = await self.alive_checks.start(now, participants, force_type="alive")
            if check is None:
                # A roll call is already running — the Culling has nothing to add.
                log.info("Culling skipped: a roll call is already running")
                self.schedule_next(now)
                return None
            state = HellEventState.COMPLETED
            end_ts = now
            meta["check_id"] = check.check_id
            affected = [p.user_id for p in participants]
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_CULLING_START",
                    "⚔️ **HELL EVENT — THE CULLING**\n\nHell demands proof of life **right now**.\nAn immediate roll call has been triggered: reply **Yes** in time or be disconnected from the VC.",
                ),
            )
        else:
            ann_text = f"⚡ Hell Event {mod.name} has started."

        # Secret events keep their identity hidden: the announcement only says
        # that *something* happened — the reveal comes when the event ends.
        if secret:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_SECRET_START",
                    "🕯️ **A SECRET HELL EVENT HAS BEGUN**\n\nSomething has changed deep within Hell…\nWhat exactly? **Nobody knows — yet.**\n\nThe veil lifts when the event ends. Stay alert.",
                )
            )

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

        log.info(
            "Hell Event %s (%s) triggered (state %s)%s",
            record.name,
            record.id,
            record.state.value,
            " [SECRET]" if secret else "",
        )

        event_out = HellEventStarted(
            record=record,
            announcement_text=ann_text,
            eligible_participants=tuple(participants),
            secret=secret,
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
        elif record.event_type is HellEventType.TIME_VORTEX:
            ann_text = str(
                getattr(
                    TEXT,
                    "HELL_EVENT_TIME_VORTEX_END",
                    "🌀 **TIME VORTEX HAS CLOSED**\n\nTime flows normally again. Every second counts once more.",
                )
            )
        else:
            ann_text = f"⚡ Hell Event {record.name} has ended."

        # A secret event is only identified once it ends — that is the reveal.
        if is_secret_record(record):
            ann_text = say(
                getattr(
                    TEXT,
                    "HELL_EVENT_SECRET_REVEAL",
                    "🕯️ **THE SECRET EVENT IS REVEALED: {name}**\n\nThe veil lifts — all along, it was **{name}**.\n\n{description}",
                ),
                name=record.name,
                description=ann_text,
            )

        log.info(
            "Hell Event %s (%s) ended%s",
            record.name,
            record.id,
            " [secret revealed]" if "secret" in record.metadata else "",
        )

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
        """Return personal leaderboard time multiplier (2.0x during Double Time, 0.5x in a Time Vortex)."""
        if self.active_event is not None and self.active_event.event_type in (
            HellEventType.DOUBLE_TIME,
            HellEventType.TIME_VORTEX,
        ):
            cur_now = now if now is not None else now_ts()
            if cur_now < self.active_event.end_ts:
                return float(self.active_event.metadata.get("multiplier", 1.0))
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

    def is_secret_active(self, now: Optional[float] = None) -> bool:
        """Check if the active Hell Event is a SECRET one (identity hidden)."""
        if self.active_event is None:
            return False
        cur_now = now if now is not None else now_ts()
        return is_secret_record(self.active_event) and cur_now < self.active_event.end_ts

    def pick_secret_event_type(self) -> HellEventType:
        """A random event type suitable for a SECRET event (must have a duration)."""
        return self.rng.choice(TIMED_EVENT_TYPES)

    def history(self) -> list[dict]:
        if not self._event_uid:
            return []
        return self.store.get_hell_events_history(self._event_uid)
