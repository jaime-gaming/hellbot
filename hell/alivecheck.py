"""Alive checks ("roll call") and Dead checks.

At a random interval (scaled by current Difficulty, 1-6h down to 1-2h) the bot
pings everybody currently in the VC:
- In an **Alive Check**: demands a `Yes` within 5 minutes. Whoever stays silent
  is disconnected from the voice channel — they keep every second of leaderboard
  time they earned and may rejoin immediately.
- In a **Dead Check** (introduced at Difficulty 2+ / 64h+): demands everyone to
  STAY SILENT. If a user replies `Yes`, they are trapped and muted from the server
  (for 1 min at Diff 2, 1-5 min at Diff 3, 5-15 min at Diff 4).

This module is **Discord-free**: it talks to the outside world through the
:class:`AliveCheckIO` interface (implemented by :mod:`hell.aliveio`), which is
what makes the whole flow unit-testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .config import Config
from .difficulty import DifficultyInfo, get_difficulty, get_difficulty_by_level
from .models import ParticipantRef
from .storage import Store
from .texts import TEXT, say
from .timeutil import format_hm, now_ts

log = logging.getLogger("hell.alivecheck")

ACCEPTED_REPLY = "yes"
_PUNCT = " \t.!¡?¿,;:\"'`"

# More ways to say Yes (case-insensitive, punctuation ignored).
YES_REPLIES = frozenset(
    {
        "yes", "yep", "yup", "yeah", "ya", "y", "sí", "si", "sì", "yes please",
        "yes sir", "aye", "affirmative", "correct", "sure", "ok", "okay", "all good",
        "i am alive", "im alive", "i'm alive",
    }
)
# Ways to say No (results in an immediate kick + "Alright then").
# Single words match per-word; multi-word entries match the whole reply.
NO_REPLIES = frozenset(
    {
        "no", "nope", "n", "no thanks", "no thank you",
        "negative", "not me",
        # common English no-synonyms
        "nah", "nahh", "nay", "nop", "never", "false", "denied",
        "hell no", "no way", "not at all", "not today", "not alive", "not here",
        "absolutely not", "definitely not", "certainly not", "of course not",
        "i refuse", "nope nope",
        # Spanish no-synonyms
        "jamas", "nunca", "negativo",
        "para nada", "claro que no", "por supuesto que no",
        "de ninguna manera", "en absoluto",
        # negated yes-words — without these, "not sure" would match the
        # word-level YES check ("sure") and count as alive
        "not sure", "not really", "not okay", "not ok", "not correct",
    }
)


def _normalize_reply(content: str) -> str:
    return content.strip().strip(_PUNCT).casefold()


def _reply_words(content: str) -> list[str]:
    return [w for w in _normalize_reply(content).split() if w]


def classify_reply(content: str) -> Optional[str]:
    """Classify a roll-call answer as ``"yes"``, ``"no"`` or ``None``."""
    normalized = _normalize_reply(content)
    if not normalized:
        return None
    if normalized in NO_REPLIES or any(w in NO_REPLIES for w in _reply_words(content)):
        return "no"
    if normalized in YES_REPLIES or any(w in YES_REPLIES for w in _reply_words(content)):
        return "yes"
    return None


def is_valid_reply(content: str, *, strict: bool = False) -> bool:
    """Does this message count as answering the roll call?

    Non-strict (default) accepts the YES_REPLIES set (`Yes`, `yeah`, `yep`, `si`,
    ``okay``, …) and tolerates trailing punctuation/whitespace.  Strict mode
    still requires the exact string `Yes`.
    """
    if strict:
        return content.strip() == "Yes"
    return classify_reply(content) == "yes"


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
    check_type: str = "alive"                # "alive" or "dead"
    mute_duration: int = 60
    trapped: set[int] = field(default_factory=set)
    muted: set[int] = field(default_factory=set)
    declined: set[int] = field(default_factory=set)        # said No on an alive check
    declined_kicked: set[int] = field(default_factory=set) # of the above, actually removed from VC

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
            "check_type": self.check_type,
            "mute_duration": self.mute_duration,
            "trapped": sorted(self.trapped),
            "muted": sorted(self.muted),
            "declined": sorted(self.declined),
            "declined_kicked": sorted(self.declined_kicked),
        }

    @classmethod
    def from_row(cls, row: dict) -> PendingCheck:
        return cls(
            check_id=row["check_id"],
            started_ts=float(row["started_ts"]),
            deadline_ts=float(row["deadline_ts"]),
            required={int(k): v for k, v in dict(row.get("required", {})).items()},
            responded={int(u) for u in row.get("responded", [])},
            channel_id=row.get("channel_id"),
            message_id=row.get("message_id"),
            check_type=str(row.get("check_type", "alive")),
            mute_duration=int(row.get("mute_duration", 60)),
            trapped={int(u) for u in row.get("trapped", [])},
            muted={int(u) for u in row.get("muted", [])},
            declined={int(u) for u in row.get("declined", [])},
            declined_kicked={int(u) for u in row.get("declined_kicked", [])},
        )


@dataclass
class CheckResult:
    check_id: str
    responded: list[ParticipantRef]
    kicked: list[ParticipantRef]
    left_early: list[ParticipantRef]
    cancelled: bool = False
    reason: str = ""
    emptied_vc: bool = False
    check_type: str = "alive"
    mute_duration: int = 60
    trapped: list[ParticipantRef] = field(default_factory=list)


class AliveCheckIO(Protocol):
    """Everything the alive check needs from Discord."""

    async def send_check(self, text: str, user_ids: Sequence[int]) -> Optional[tuple[int, int]]:
        """Post the roll call, pinging `user_ids`.  Returns (channel_id, message_id)."""

    async def send_result(self, text: str) -> None:
        """Post the outcome summary."""

    async def kick(self, user_ids: Sequence[int], reason: str) -> list[int]:
        """Disconnect users from the VC.  Returns the ids actually removed."""

    async def mute(self, user_id: int, duration_seconds: int, reason: str) -> bool:
        """Mute / timeout a user on the server for the specified duration."""

    async def replies_since(
        self, channel_id: int, message_id: int, user_ids: Sequence[int]
    ) -> set[int]:
        """Read back replies posted while the bot was offline."""


# ------------------------------------------------------------------ manager


class AliveCheckManager:
    """Schedules, runs and resolves roll calls and dead checks for the current event."""

    def __init__(
        self,
        config: Config,
        store: Store,
        io: AliveCheckIO,
        *,
        rng: Optional[random.Random] = None,
        engine: Optional[Any] = None,
    ):
        self.config = config
        self.store = store
        self.io = io
        self.rng = rng or random.Random()
        self.engine = engine
        self.pending: Optional[PendingCheck] = None
        self._event_uid: Optional[str] = None
        self._active_tasks: set[Any] = set()
        self._resolve_lock = asyncio.Lock()
        # Players kicked for saying "No" — they get a friendly DM when they
        # rejoin the VC (they don't have to answer the roll call to return).
        self._no_kicked: set[int] = set()

    # ------------------------------------------------------------- lifecycle

    def bind(self, event_uid: Optional[str], *, now: Optional[float] = None) -> None:
        """Attach to an event, restoring any pending check from the database."""
        self._event_uid = event_uid
        self.pending = None
        self._no_kicked = set()
        if not event_uid:
            return
        row = self.store.load_alive_check(event_uid)
        if row:
            self.pending = PendingCheck.from_row(row)
        self._load_no_kicked()
        if self.store.get_next_alive_check(event_uid) is None:
            self.schedule_next(now if now is not None else now_ts())

    @property
    def bound_uid(self) -> Optional[str]:
        """Which event this manager is currently attached to."""
        return self._event_uid

    def reset(self) -> None:
        self.pending = None
        self._event_uid = None
        self._no_kicked = set()

    # ------------------------------------------------------ no-kick rejoin DMs

    def note_no_kick(self, user_id: int) -> None:
        """Remember a player kicked for saying No — they get a DM on rejoin."""
        if user_id in self._no_kicked:
            return
        self._no_kicked.add(user_id)
        self._save_no_kicked()

    def pop_no_kick_rejoins(self, present_ids: AbstractSet[int]) -> list[int]:
        """Consume the no-kicked players that are (back) in the VC right now.

        Returns the user IDs to DM and removes them from the tracker, so the
        message is sent once per kick — not on every monitor tick.
        """
        rejoined = sorted(uid for uid in self._no_kicked if uid in present_ids)
        if rejoined:
            self._no_kicked.difference_update(rejoined)
            self._save_no_kicked()
        return rejoined

    def _no_kicked_key(self) -> str:
        return f"no_kicked:{self._event_uid or ''}"

    def _save_no_kicked(self) -> None:
        if not self._event_uid:
            return
        try:
            self.store.set_meta(self._no_kicked_key(), json.dumps(sorted(self._no_kicked)))
        except Exception:  # pragma: no cover - never break a kick on a meta write
            log.warning("Could not persist the no-kick tracker", exc_info=True)

    def _load_no_kicked(self) -> None:
        try:
            raw = self.store.get_meta(self._no_kicked_key())
            self._no_kicked = {int(u) for u in json.loads(raw)} if raw else set()
        except Exception:  # pragma: no cover - corrupt meta never breaks binding
            log.warning("Could not read the no-kick tracker", exc_info=True)
            self._no_kicked = set()

    # ------------------------------------------------------------ difficulties

    def current_difficulty(self, now: Optional[float] = None) -> DifficultyInfo:
        """Get the current difficulty tier based on elapsed event time or override."""
        if self.engine is not None and getattr(self.engine, "status", None) and self.engine.status.is_active:
            override = getattr(self.engine, "difficulty_override", None)
            elapsed = self.engine.elapsed(now)
            return get_difficulty(elapsed, override=override)
        return get_difficulty_by_level(0)

    # ------------------------------------------------------------ scheduling

    @property
    def enabled(self) -> bool:
        return self.config.alive_check_enabled

    def pick_delay(self, now: Optional[float] = None) -> float:
        """A random delay scaled by current difficulty (1-6h, 1-5h, 1-4h, 1-3h, 1-2h)."""
        diff = self.current_difficulty(now)
        min_hours = min(self.config.alive_check_min_hours, diff.min_check_hours)
        max_hours = min(self.config.alive_check_max_hours, diff.max_check_hours)
        low = min(min_hours, max_hours) * 3600.0
        high = max(min_hours, max_hours) * 3600.0
        return self.rng.uniform(low, high)

    def schedule_next(self, now: float) -> float:
        delay = self.pick_delay(now)
        when = now + delay
        if self._event_uid:
            self.store.set_next_alive_check(self._event_uid, when)
        log.info("Next roll call in %s (at %.0f)", format_hm(delay), when)
        return when

    def shift_schedule(self, duration: float) -> None:
        """Move the next (and any pending) roll call forward by ``duration`` seconds.

        Used when a failed/paused run is resumed so Discord timestamps and the
        due clock stay aligned with wall time instead of firing immediately.
        """
        if not self._event_uid or duration <= 0:
            return
        if self.pending is not None:
            self.pending.started_ts += duration
            self.pending.deadline_ts += duration
            self.store.save_alive_check(self._event_uid, self.pending.to_row())
        next_ts = self.next_check_ts()
        if next_ts is not None:
            self.store.set_next_alive_check(self._event_uid, next_ts + duration)
            log.info("Shifted next roll call forward by %.1fs", duration)
        else:
            self.schedule_next(now_ts())

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
            # If every required user has answered, finish right away instead of
            # making them wait for the rest of the timeout window.
            if self.pending.check_type == "alive" and not self.pending.missing:
                return await self.resolve(now, participants)
            if now >= self.pending.deadline_ts:
                return await self.resolve(now, participants)
            return None

        if self.is_due(now) and participants:
            await self.start(now, participants)
        return None

    # ----------------------------------------------------------------- start

    async def start(
        self,
        now: float,
        participants: Sequence[ParticipantRef],
        *,
        force_type: Optional[str] = None,
    ) -> Optional[PendingCheck]:
        """Post the roll call (or dead check) and start the clock."""
        if self.pending is not None or not self._event_uid or not participants:
            return None

        diff = self.current_difficulty(now)
        if force_type is not None:
            check_type = force_type
        elif diff.dead_checks_enabled and (self.rng.random() < diff.dead_check_chance):
            check_type = "dead"
        else:
            check_type = "alive"

        mute_duration = 60
        if check_type == "dead":
            if diff.min_mute_seconds < diff.max_mute_seconds:
                mute_duration = self.rng.randint(diff.min_mute_seconds, diff.max_mute_seconds)
            else:
                mute_duration = diff.min_mute_seconds or 60

        timeout = max(30.0, self.config.alive_check_timeout_minutes * 60.0)
        check = PendingCheck(
            check_id=uuid.uuid4().hex[:12],
            started_ts=now,
            deadline_ts=now + timeout,
            required={p.user_id: p.display_name for p in participants},
            check_type=check_type,
            mute_duration=mute_duration,
        )
        # 1) Persist to DB first (tentative — no channel/message IDs yet).
        self.pending = check
        self.store.save_alive_check(self._event_uid, check.to_row())

        # 2) Send to Discord.
        try:
            sent = await self.io.send_check(self.render_check(check), list(check.required))
        except Exception:
            log.exception("%s check could not be posted — cleaning up and rescheduling", check_type.title())
            sent = None
        if sent is None:
            log.error("%s check could not be posted — cleaning up and rescheduling", check_type.title())
            self.store.clear_alive_check(self._event_uid)
            self.pending = None
            self.schedule_next(now)
            return None

        # 3) Update the DB record with the real Discord coordinates.
        check.channel_id, check.message_id = sent
        self.store.save_alive_check(self._event_uid, check.to_row())
        log.info(
            "%s check %s started for %d user(s); deadline in %.0fs (mute=%ds)",
            check.check_type.title(),
            check.check_id,
            len(check.required),
            timeout,
            mute_duration,
        )
        return check

    # -------------------------------------------------------------- replies

    def register_reply(self, user_id: int, content: str, channel_id: Optional[int] = None) -> bool:
        """Record an answer. Returns True when it counted for a pending check.

        A ``Yes`` (or one of the accepted variants) marks the user as answered.
        A ``No`` also counts as answered for check-completion purposes and is
        marked as declined so :meth:`process_reply` / :meth:`resolve` can kick
        them from the VC and say "Alright then".
        """
        check = self.pending
        if check is None or not self._event_uid:
            return False
        if channel_id is not None and check.channel_id is not None and channel_id != check.channel_id:
            return False
        if user_id not in check.required or user_id in check.responded:
            return False

        answer = classify_reply(content)
        if answer is None:
            return False
        # In strict mode only the exact word "Yes" counts as a positive answer.
        if answer == "yes" and not is_valid_reply(content, strict=self.config.alive_check_strict):
            return False

        check.responded.add(user_id)
        if answer == "no":
            check.declined.add(user_id)
        if check.check_type == "dead":
            check.trapped.add(user_id)
            if hasattr(self.io, "mute") and user_id not in check.muted:
                check.muted.add(user_id)
                try:
                    loop = asyncio.get_running_loop()
                    t = loop.create_task(
                        self.io.mute(
                            user_id,
                            check.mute_duration,
                            f"Welcome to Hell: replied to a dead check ({check.check_id})",
                        )
                    )
                    self._active_tasks.add(t)
                    t.add_done_callback(self._active_tasks.discard)
                except RuntimeError:
                    pass  # No running event loop in sync unit tests

        self.store.save_alive_check(self._event_uid, check.to_row())
        log.info(
            "%s check %s: %s replied (%d/%d)%s",
            check.check_type.title(),
            check.check_id,
            check.required.get(user_id, user_id),
            len(check.responded),
            len(check.required),
            " [TRAPPED & MUTED]" if check.check_type == "dead" else (
                " [DECLINED]" if answer == "no" else ""
            ),
        )
        return True

    async def process_reply(
        self, user_id: int, content: str, channel_id: Optional[int] = None
    ) -> Optional[str]:
        """Handle a reply with real Discord side effects.

        Returns ``"yes"``, ``"no"``, ``"invalid"`` or ``"ignored"``.  On a
        ``"no"`` it kicks the player and posts "Alright then" immediately; once
        everyone has answered it resolves the alive check right away.
        """
        answer = classify_reply(content)
        if answer is None:
            return "invalid"
        if not self.register_reply(user_id, content, channel_id):
            return "ignored"
        check = self.pending
        if check is None:
            return answer
        if check.check_type == "alive" and answer == "no":
            await self._handle_no(user_id)
        await self._resolve_if_complete()
        return answer

    async def _handle_no(self, user_id: int) -> None:
        """Kick a player who said No and reply "Alright then" in the channel."""
        check = self.pending
        if check is None or check.check_type != "alive":
            return
        reason = "Welcome to Hell: said no to the alive check"
        kicked = []
        try:
            kicked = await self.io.kick([user_id], reason)
        except Exception:  # pragma: no cover - never break on a reply
            log.exception("Could not disconnect %s after a No reply", user_id)
        if kicked:
            check.declined_kicked.add(user_id)
            self.note_no_kick(user_id)
            self.store.save_alive_check(self._event_uid or "", check.to_row())
        await self.io.send_result(str(getattr(TEXT, "ALIVE_CHECK_NO_REPLY", "Alright then")))

    async def _resolve_if_complete(self) -> Optional[CheckResult]:
        """Resolve an alive check as soon as every required user has answered."""
        check = self.pending
        if check is None:
            return None
        if check.check_type != "alive" or check.missing:
            return None
        # Use the monitor's last-known VC members so an early resolution can
        # tell the engine when a No kick (or ack + immediate resolve) empties it.
        participants = ()
        if self.engine is not None:
            try:
                participants = self.engine.last_participants
            except (AttributeError, TypeError):  # pragma: no cover - defensive
                participants = ()
        return await self.resolve(now_ts(), participants)

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
            log.exception("Could not backfill replies")
            return 0
        new = {uid for uid in found if uid in check.required} - check.responded
        if new and self._event_uid:
            check.responded |= new
            if check.check_type == "dead":
                check.trapped |= new
            self.store.save_alive_check(self._event_uid, check.to_row())
            log.info("%s check %s: recovered %d reply(ies) after restart", check.check_type.title(), check.check_id, len(new))
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
        """Deadline reached (or everyone answered): resolve check outcomes."""
        async with self._resolve_lock:
            check = self.pending
            if check is None or not self._event_uid:
                return None

            present = {p.user_id for p in participants}
            responded = [ParticipantRef(uid, check.required[uid]) for uid in sorted(check.responded)]
            silent = sorted(check.missing)
            left_early = [ParticipantRef(uid, check.required[uid]) for uid in silent if uid not in present]

            if check.check_type == "dead":
                # For dead checks: silent users are SAFE; nobody is kicked from VC!
                to_kick: list[int] = []
                trapped_refs = [ParticipantRef(uid, check.required[uid]) for uid in sorted(check.trapped)]
                # Ensure trapped users who weren't muted yet (e.g. backfilled) are muted
                if not cancelled:
                    unmuted = [uid for uid in check.trapped if uid not in check.muted]
                    for uid in unmuted:
                        try:
                            await self.io.mute(
                                uid,
                                check.mute_duration,
                                f"Welcome to Hell: replied to a dead check ({check.check_id})",
                            )
                            check.muted.add(uid)
                        except Exception:
                            log.warning("Could not mute %s during dead check resolution", uid, exc_info=True)

                self.store.record_alive_check_history(
                    self._event_uid,
                    check_id=check.check_id,
                    started_ts=check.started_ts,
                    resolved_ts=now,
                    required=len(check.required),
                    responded=len(check.responded),
                    kicked=[],
                    cancelled=cancelled,
                )
                kicked_ids: list[int] = []
                kicked: list[ParticipantRef] = []
                emptied_vc = False
            else:
                trapped_refs = []
                to_kick = [uid for uid in silent if uid in present]
                # A "No" reply should have been kicked already; if not (e.g. it
                # came in while the bot was offline) clean it up here.
                for uid in sorted(check.declined):
                    if uid not in check.declined_kicked and uid in present:
                        to_kick.append(uid)

                # 1) Save history to DB BEFORE any Discord I/O.
                self.store.record_alive_check_history(
                    self._event_uid,
                    check_id=check.check_id,
                    started_ts=check.started_ts,
                    resolved_ts=now,
                    required=len(check.required),
                    responded=len(check.responded),
                    kicked=list(to_kick) if not cancelled else [],
                    cancelled=cancelled,
                )

                # 2) Disconnect silent users (and declining users still present)
                kicked_ids = []
                if not cancelled and to_kick:
                    kicked_ids = await self.io.kick(
                        to_kick, "Welcome to Hell: no answer to the alive check"
                    )
                    for uid in sorted(to_kick):
                        if uid in check.declined and uid in kicked_ids:
                            check.declined_kicked.add(uid)
                            self.note_no_kick(uid)
                if to_kick and not kicked_ids and not cancelled:
                    log.error(
                        "Alive check %s: %d user(s) ignored it but none could be disconnected",
                        check.check_id,
                        len(to_kick),
                    )
                all_kicked = set(kicked_ids) | set(check.declined_kicked)
                kicked = [ParticipantRef(uid, check.required[uid]) for uid in sorted(all_kicked)]
                # ``all_kicked`` includes players already kicked by an immediate
                # "No" reply, so an early resolve still detects an emptied VC.
                emptied_vc = bool(
                    not cancelled
                    and all_kicked
                    and ((not present) or (present <= all_kicked))
                )
                if emptied_vc and self._event_uid:
                    self.store.set_alive_check_emptied(self._event_uid, now)
                    if self.engine is not None:
                        self.engine.notify_alive_check_emptied(now)

            self.store.clear_alive_check(self._event_uid)
            self.pending = None
            self.schedule_next(now)

            result = CheckResult(
                check_id=check.check_id,
                responded=responded,
                kicked=kicked,
                left_early=left_early,
                cancelled=cancelled,
                reason=reason,
                emptied_vc=emptied_vc,
                check_type=check.check_type,
                mute_duration=check.mute_duration,
                trapped=trapped_refs,
            )

            log.info(
                "%s check %s resolved: %d/%d answered, %d disconnected, %d trapped%s",
                check.check_type.title(),
                check.check_id,
                len(responded),
                len(check.required),
                len(kicked),
                len(trapped_refs),
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
        if check and check.check_type == "dead":
            mute_str = format_hm(check.mute_duration) if check.mute_duration >= 60 else f"{check.mute_duration}s"
            return TEXT.DEAD_CHECK_TEXT + "\n" + say(
                TEXT.DEAD_CHECK_INSTRUCTIONS,
                minutes=minutes,
                mute_duration=mute_str,
            )
        return TEXT.ALIVE_CHECK_TEXT + "\n" + say(TEXT.ALIVE_CHECK_INSTRUCTIONS, minutes=minutes)

    def render_result(self, result: CheckResult) -> str:
        if result.cancelled:
            return say(
                TEXT.ALIVE_CHECK_CANCELLED,
                reason=result.reason or TEXT.ALIVE_CHECK_CANCELLED_DEFAULT_REASON,
            )
        if result.check_type == "dead":
            mute_str = format_hm(result.mute_duration) if result.mute_duration >= 60 else f"{result.mute_duration}s"
            lines = [TEXT.DEAD_CHECK_RESULT_TITLE]
            if result.trapped:
                lines.append(
                    say(
                        TEXT.DEAD_CHECK_RESULT_TRAPPED,
                        trapped=", ".join(k.mention() for k in result.trapped),
                        mute_duration=mute_str,
                    )
                )
            else:
                lines.append(TEXT.DEAD_CHECK_RESULT_NOBODY_TRAPPED)
            return "\n".join(lines)

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
        diff = self.current_difficulty(now)
        if self.pending is not None:
            if self.pending.check_type == "dead":
                return say(
                    TEXT.DEAD_CHECK_STATUS_RUNNING,
                    left=int(self.pending.seconds_left(now)),
                )
            return say(
                TEXT.ALIVE_CHECK_STATUS_RUNNING,
                answered=len(self.pending.responded),
                total=len(self.pending.required),
                left=int(self.pending.seconds_left(now)),
            )
        return say(
            TEXT.ALIVE_CHECK_STATUS_IDLE,
            min_hours=min(self.config.alive_check_min_hours, diff.min_check_hours),
            max_hours=min(self.config.alive_check_max_hours, diff.max_check_hours),
            minutes=int(self.config.alive_check_timeout_minutes),
        )
