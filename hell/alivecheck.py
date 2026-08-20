"""Alive checks ("roll call").

At a random interval between 1 and 6 hours the bot pings everybody currently
in the VC and demands a `Yes` within 5 minutes.  Whoever stays silent is
disconnected from the voice channel — they keep every second of leaderboard
time they earned and may rejoin immediately.

This module is **Discord-free**: it talks to the outside world through the
:class:`AliveCheckIO` interface (implemented by :mod:`hell.aliveio`), which is
what makes the whole flow unit-testable.

Rules implemented here
----------------------
* Bots and `@clanker` users are never part of a check (they never reach this
  module — the monitor filters them out first).
* Only users who were in the VC when the check started must answer; joining
  mid-check does not put you on the hook, and leaving mid-check simply means
  there is nobody to disconnect.
* Being disconnected never removes leaderboard time and never, by itself,
  fails the event: the run only ends if the VC is left with no valid humans
  at all (the normal failure rule, evaluated by the engine).
* A check that was interrupted by a bot restart is **cancelled** rather than
  enforced — nobody gets kicked because the bot was offline.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from .config import Config
from .models import ParticipantRef
from .storage import Store
from .texts import TEXT, say
from .timeutil import format_hm, now_ts

log = logging.getLogger("hell.alivecheck")

ACCEPTED_REPLY = "yes"


def check_text() -> str:
    """The exact roll-call headline (edit it in Announcements.py)."""
    return TEXT.ALIVE_CHECK_TEXT


def __getattr__(name: str):
    """`CHECK_TEXT` stays importable but always reflects Announcements.py."""
    if name == "CHECK_TEXT":
        return TEXT.ALIVE_CHECK_TEXT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ------------------------------------------------------------------- models


@dataclass
class PendingCheck:
    """A roll call that is currently running."""

    check_id: str
    started_ts: float
    deadline_ts: float
    required: dict[int, str]                 # user_id -> display name at check time
    responded: set[int] = field(default_factory=set)
    channel_id: Optional[int] = None
    message_id: Optional[int] = None

    @property
    def missing(self) -> set[int]:
        return set(self.required) - self.responded

    def seconds_left(self, now: float) -> float:
        return max(0.0, self.deadline_ts - now)

    def to_row(self) -> dict:
        return {
            "check_id": self.check_id,
            "started_ts": self.started_ts,
            "deadline_ts": self.deadline_ts,
            "required": {str(k): v for k, v in self.required.items()},
            "responded": sorted(self.responded),
            "channel_id": self.channel_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_row(cls, row: dict) -> "PendingCheck":
        return cls(
            check_id=row["check_id"],
            started_ts=float(row["started_ts"]),
            deadline_ts=float(row["deadline_ts"]),
            required={int(k): v for k, v in dict(row.get("required", {})).items()},
            responded={int(u) for u in row.get("responded", [])},
            channel_id=row.get("channel_id"),
            message_id=row.get("message_id"),
        )


@dataclass
class CheckResult:
    check_id: str
    responded: list[ParticipantRef]
    kicked: list[ParticipantRef]
    left_early: list[ParticipantRef]
    cancelled: bool = False
    reason: str = ""


class AliveCheckIO(Protocol):
    """Everything the alive check needs from Discord."""

    async def send_check(self, text: str, user_ids: Sequence[int]) -> Optional[tuple[int, int]]:
        """Post the roll call, pinging `user_ids`.  Returns (channel_id, message_id)."""

    async def send_result(self, text: str) -> None:
        """Post the outcome summary."""

    async def kick(self, user_ids: Sequence[int], reason: str) -> list[int]:
        """Disconnect users from the VC.  Returns the ids actually removed."""

    async def replies_since(
        self, channel_id: int, message_id: int, user_ids: Sequence[int]
    ) -> set[int]:
        """Read back replies posted while the bot was offline."""


# ------------------------------------------------------------------- helpers


def is_valid_reply(content: str, *, strict: bool = False) -> bool:
    """Does this message count as answering the roll call?

    Non-strict (default) accepts `Yes`, `yes`, `YES`, and tolerates trailing
    punctuation/whitespace.  Strict mode requires the exact string `Yes`.
    """
    if strict:
        return content.strip() == "Yes"
    return content.strip().strip(".!¡?¿,;:").casefold() == ACCEPTED_REPLY


# ------------------------------------------------------------------ manager


class AliveCheckManager:
    """Schedules, runs and resolves roll calls for the current event."""

    def __init__(
        self,
        config: Config,
        store: Store,
        io: AliveCheckIO,
        *,
        rng: Optional[random.Random] = None,
    ):
        self.config = config
        self.store = store
        self.io = io
        self.rng = rng or random.Random()
        self.pending: Optional[PendingCheck] = None
        self._event_uid: Optional[str] = None

    # ------------------------------------------------------------- lifecycle

    def bind(self, event_uid: Optional[str], *, now: Optional[float] = None) -> None:
        """Attach to an event, restoring any pending check from the database."""
        self._event_uid = event_uid
        self.pending = None
        if not event_uid:
            return
        row = self.store.load_alive_check(event_uid)
        if row:
            self.pending = PendingCheck.from_row(row)
        if self.store.get_next_alive_check(event_uid) is None:
            self.schedule_next(now if now is not None else now_ts())

    def reset(self) -> None:
        self.pending = None
        self._event_uid = None

    # ------------------------------------------------------------ scheduling

    @property
    def enabled(self) -> bool:
        return self.config.alive_check_enabled

    def pick_delay(self) -> float:
        """A random delay in the configured window (default 1–6 hours)."""
        low = min(self.config.alive_check_min_hours, self.config.alive_check_max_hours) * 3600.0
        high = max(self.config.alive_check_min_hours, self.config.alive_check_max_hours) * 3600.0
        return self.rng.uniform(low, high)

    def schedule_next(self, now: float) -> float:
        delay = self.pick_delay()
        when = now + delay
        if self._event_uid:
            self.store.set_next_alive_check(self._event_uid, when)
        log.info("Next alive check in %s (at %.0f)", format_hm(delay), when)
        return when

    def next_check_ts(self) -> Optional[float]:
        if not self._event_uid:
            return None
        return self.store.get_next_alive_check(self._event_uid)

    def is_due(self, now: float) -> bool:
        if not self.enabled or self.pending is not None or not self._event_uid:
            return False
        due = self.next_check_ts()
        if due is None:
            self.schedule_next(now)
            return False
        return now >= due

    # ------------------------------------------------------------------ tick

    async def tick(self, now: float, participants: Sequence[ParticipantRef]) -> Optional[CheckResult]:
        """Called once per second by the monitor while the event is RUNNING."""
        if not self.enabled or not self._event_uid:
            return None

        if self.pending is not None:
            if now >= self.pending.deadline_ts:
                return await self.resolve(now, participants)
            return None

        if self.is_due(now) and participants:
            await self.start(now, participants)
        return None

    # ----------------------------------------------------------------- start

    async def start(self, now: float, participants: Sequence[ParticipantRef]) -> Optional[PendingCheck]:
        """Post the roll call and start the 5-minute clock."""
        if self.pending is not None or not self._event_uid or not participants:
            return None

        timeout = max(30.0, self.config.alive_check_timeout_minutes * 60.0)
        check = PendingCheck(
            check_id=uuid.uuid4().hex[:12],
            started_ts=now,
            deadline_ts=now + timeout,
            required={p.user_id: p.display_name for p in participants},
        )
        sent = await self.io.send_check(self.render_check(check), list(check.required))
        if sent is None:
            # Could not post: try again on the next scheduling window rather
            # than silently kicking people who never saw a message.
            log.error("Alive check could not be posted — rescheduling")
            self.schedule_next(now)
            return None
        check.channel_id, check.message_id = sent

        self.pending = check
        self.store.save_alive_check(self._event_uid, check.to_row())
        log.info(
            "Alive check %s started for %d user(s); deadline in %.0fs",
            check.check_id,
            len(check.required),
            timeout,
        )
        return check

    # -------------------------------------------------------------- replies

    def register_reply(self, user_id: int, content: str, channel_id: Optional[int] = None) -> bool:
        """Record a `Yes`.  Returns True when it counted for a pending check."""
        check = self.pending
        if check is None or not self._event_uid:
            return False
        if channel_id is not None and check.channel_id is not None and channel_id != check.channel_id:
            return False
        if user_id not in check.required or user_id in check.responded:
            return False
        if not is_valid_reply(content, strict=self.config.alive_check_strict):
            return False
        check.responded.add(user_id)
        self.store.save_alive_check(self._event_uid, check.to_row())
        log.info(
            "Alive check %s: %s replied (%d/%d)",
            check.check_id,
            check.required.get(user_id, user_id),
            len(check.responded),
            len(check.required),
        )
        return True

    async def backfill_replies(self) -> int:
        """After a restart, read the channel for answers posted while offline."""
        check = self.pending
        if check is None or check.channel_id is None or check.message_id is None:
            return 0
        try:
            found = await self.io.replies_since(
                check.channel_id, check.message_id, list(check.missing)
            )
        except Exception:  # pragma: no cover - never break recovery
            log.exception("Could not backfill alive-check replies")
            return 0
        new = {uid for uid in found if uid in check.required} - check.responded
        if new and self._event_uid:
            check.responded |= new
            self.store.save_alive_check(self._event_uid, check.to_row())
            log.info("Alive check %s: recovered %d reply(ies) after restart", check.check_id, len(new))
        return len(new)

    # --------------------------------------------------------------- resolve

    async def resolve(
        self,
        now: float,
        participants: Sequence[ParticipantRef],
        *,
        cancelled: bool = False,
        reason: str = "",
    ) -> Optional[CheckResult]:
        """Deadline reached: disconnect whoever stayed silent."""
        check = self.pending
        if check is None or not self._event_uid:
            return None

        present = {p.user_id for p in participants}
        responded = [ParticipantRef(uid, check.required[uid]) for uid in sorted(check.responded)]
        silent = sorted(check.missing)
        # Someone who already left the VC has nothing to be kicked from.
        to_kick = [uid for uid in silent if uid in present]
        left_early = [ParticipantRef(uid, check.required[uid]) for uid in silent if uid not in present]

        kicked_ids: list[int] = []
        if not cancelled and to_kick:
            kicked_ids = await self.io.kick(
                to_kick, "Welcome to Hell: no answer to the alive check"
            )
        kicked = [ParticipantRef(uid, check.required[uid]) for uid in kicked_ids]

        result = CheckResult(
            check_id=check.check_id,
            responded=responded,
            kicked=kicked,
            left_early=left_early,
            cancelled=cancelled,
            reason=reason,
        )

        self.store.record_alive_check_history(
            self._event_uid,
            check_id=check.check_id,
            started_ts=check.started_ts,
            resolved_ts=now,
            required=len(check.required),
            responded=len(check.responded),
            kicked=[k.user_id for k in kicked],
            cancelled=cancelled,
        )
        self.store.clear_alive_check(self._event_uid)
        self.pending = None
        self.schedule_next(now)

        log.info(
            "Alive check %s resolved: %d/%d answered, %d disconnected%s",
            check.check_id,
            len(responded),
            len(check.required),
            len(kicked),
            " (cancelled)" if cancelled else "",
        )
        await self.io.send_result(self.render_result(result))
        return result

    async def cancel(self, now: float, reason: str) -> Optional[CheckResult]:
        """Abandon a check without kicking anybody (used after a restart)."""
        return await self.resolve(now, [], cancelled=True, reason=reason)

    # -------------------------------------------------------------- rendering

    def render_check(self, check: Optional[PendingCheck] = None) -> str:
        minutes = int(max(30.0, self.config.alive_check_timeout_minutes * 60.0) // 60)
        return TEXT.ALIVE_CHECK_TEXT + "\n" + say(TEXT.ALIVE_CHECK_INSTRUCTIONS, minutes=minutes)

    def render_result(self, result: CheckResult) -> str:
        if result.cancelled:
            return say(
                TEXT.ALIVE_CHECK_CANCELLED,
                reason=result.reason or TEXT.ALIVE_CHECK_CANCELLED_DEFAULT_REASON,
            )
        lines = [
            TEXT.ALIVE_CHECK_RESULT_TITLE,
            say(TEXT.ALIVE_CHECK_RESULT_ANSWERED, answered=len(result.responded)),
        ]
        if result.kicked:
            lines.append(
                say(
                    TEXT.ALIVE_CHECK_RESULT_KICKED,
                    kicked=", ".join(k.mention() for k in result.kicked),
                )
            )
            lines.append(TEXT.ALIVE_CHECK_RESULT_KICKED_NOTE)
        else:
            lines.append(TEXT.ALIVE_CHECK_RESULT_NOBODY_KICKED)
        if result.left_early:
            lines.append(
                say(
                    TEXT.ALIVE_CHECK_RESULT_LEFT_EARLY,
                    left_early=", ".join(p.mention() for p in result.left_early),
                )
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ reporting

    def status_line(self, now: float) -> Optional[str]:
        """Short description for `/hell status` (never reveals the next time)."""
        if not self.enabled:
            return None
        if self.pending is not None:
            return say(
                TEXT.ALIVE_CHECK_STATUS_RUNNING,
                answered=len(self.pending.responded),
                total=len(self.pending.required),
                left=int(self.pending.seconds_left(now)),
            )
        return say(
            TEXT.ALIVE_CHECK_STATUS_IDLE,
            min_hours=self.config.alive_check_min_hours,
            max_hours=self.config.alive_check_max_hours,
            minutes=int(self.config.alive_check_timeout_minutes),
        )
