"""Gambling helpers — parse bets, persist limits, decide if a roll is allowed.

The actual payout lives in :meth:`hell.cog.HellCommands._perform_gamble`; this
module is Discord-free so the rules can be unit-tested without a gateway.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .storage import Store
from .timeutil import now_ts

log = logging.getLogger("hell.gamble")

DEFAULT_BET_HOURS = 0.25
MIN_BET_HOURS = 0.25
# Fraction of a winning roll that becomes a jackpot (extra multiplier).
JACKPOT_SHARE = 0.08
JACKPOT_EXTRA = 1.0
_HIST_KEY = "gamble_hist:{event}:{user}"
_CD_KEY = "gamble_cd:{event}:{user}"

# "0.25", "1h", "15m", "30min", "90s"
_BET_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)?\s*$",
    re.IGNORECASE,
)


def parse_bet_hours(raw: Optional[object], *, default: float = DEFAULT_BET_HOURS) -> Optional[float]:
    """Turn a user bet into hours, or ``None`` if it is not a number."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    text = str(raw).strip().lower().replace(",", ".")
    match = _BET_RE.match(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "h").lower()
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        hours = amount
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        hours = amount / 60.0
    else:
        hours = amount / 3600.0
    return hours if hours > 0 else None


def effective_cooldown(diff: object, used_this_hour: int) -> tuple[float, bool]:
    """Normal short CD while under the hourly quota; overflow timer after that.

    Overflow is unlimited — you can keep gambling, just not as fast.
    """
    quota = int(getattr(diff, "gamble_hourly_limit", 0) or 0)
    overflow_cd = float(getattr(diff, "gamble_overflow_cooldown_seconds", 0.0) or 0.0)
    normal_cd = float(getattr(diff, "gamble_cooldown_seconds", 0.0) or 0.0)
    if quota > 0 and overflow_cd > 0 and used_this_hour >= quota:
        return overflow_cd, True
    return normal_cd, False


BET_STEP_HOURS = 0.25  # 15-minute chips
# Extra win multiplier at the max bet (listed multiplier is the 15m rate).
MAX_BET_MULT_BONUS = 0.5


def snap_bet_hours(hours: float) -> float:
    """Snap to 15-minute chips, never below the minimum stake."""
    steps = max(1, round(float(hours) / BET_STEP_HOURS))
    return round(steps * BET_STEP_HOURS, 6)


def _stake_t(diff: object, bet_hours: float) -> float:
    max_bet = float(getattr(diff, "gamble_max_bet_hours", MIN_BET_HOURS) or MIN_BET_HOURS)
    span = max(max_bet - MIN_BET_HOURS, 1e-9)
    return min(1.0, max(0.0, (float(bet_hours) - MIN_BET_HOURS) / span))


def win_chance_for_bet(diff: object, bet_hours: float) -> float:
    """Listed chance at the 15m stake; down to 70% of listed at max bet."""
    listed = float(getattr(diff, "gamble_win_chance", 0.0) or 0.0)
    return listed * (1.0 - 0.30 * _stake_t(diff, bet_hours))


def win_multiplier_for_bet(diff: object, bet_hours: float) -> float:
    """Listed multiplier at 15m; a little extra juice at the max stake."""
    listed = float(getattr(diff, "gamble_win_multiplier", 1.0) or 1.0)
    return listed + MAX_BET_MULT_BONUS * _stake_t(diff, bet_hours)


def loss_mute_seconds(diff: object, bet_hours: float) -> int:
    """Base mute from the difficulty, plus 1 minute per extra 15m chip.

    Mute applies to Real Timer bets only — callers must skip it on Gamble Time.
    """
    base = int(getattr(diff, "gamble_loss_mute_seconds", 60) or 60)
    extra_chips = max(0, round(float(bet_hours) / BET_STEP_HOURS) - 1)
    return base + extra_chips * 60


# Gamble Time losses take extra chips (no mute). 15m → 1.5× stake, max bet → 2.0×.
GAMBLE_TIME_LOSS_MIN = 1.5
GAMBLE_TIME_LOSS_MAX = 2.0
STARTING_GAMBLE_SECONDS = 3600.0  # everyone starts with 1h of Gamble Time


def gamble_time_loss_multiplier(diff: object, bet_hours: float) -> float:
    t = _stake_t(diff, bet_hours)
    return GAMBLE_TIME_LOSS_MIN + (GAMBLE_TIME_LOSS_MAX - GAMBLE_TIME_LOSS_MIN) * t


def format_mute(seconds: int) -> str:
    minutes = max(1, int(seconds) // 60)
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


@dataclass(frozen=True)
class GambleRoll:
    won: bool
    jackpot: bool
    multiplier: float
    win_chance: float
    mute_seconds: int


def resolve_gamble(diff: object, bet_hours: float, *, roll: float) -> GambleRoll:
    chance = win_chance_for_bet(diff, bet_hours)
    win_mult = win_multiplier_for_bet(diff, bet_hours)
    mute = loss_mute_seconds(diff, bet_hours)
    if roll >= chance:
        return GambleRoll(
            won=False, jackpot=False, multiplier=0.0, win_chance=chance, mute_seconds=mute
        )
    jackpot = roll < chance * JACKPOT_SHARE
    multiplier = win_mult + JACKPOT_EXTRA if jackpot else win_mult
    return GambleRoll(
        won=True, jackpot=jackpot, multiplier=multiplier, win_chance=chance, mute_seconds=0
    )


def format_wait(seconds: float) -> str:
    left = max(0, int(seconds))
    if left >= 60:
        return f"{left // 60}m {left % 60}s"
    return f"{left}s"


class GambleBook:
    """Per-user cooldown + rolling hourly limit, persisted in SQLite meta."""

    def __init__(self, store: Store):
        self.store = store

    def last_ts(self, event_uid: str, user_id: int) -> float:
        raw = self.store.get_meta(_CD_KEY.format(event=event_uid, user=user_id))
        try:
            return float(raw) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0

    def history(self, event_uid: str, user_id: int, *, now: Optional[float] = None) -> list[float]:
        now = now_ts() if now is None else now
        raw = self.store.get_meta(_HIST_KEY.format(event=event_uid, user=user_id))
        stamps: list[float] = []
        if raw:
            try:
                stamps = [float(t) for t in json.loads(raw)]
            except (TypeError, ValueError, json.JSONDecodeError):
                stamps = []
        return [t for t in stamps if now - t < 3600.0]

    def record(self, event_uid: str, user_id: int, now: float) -> None:
        hist = self.history(event_uid, user_id, now=now)
        hist.append(now)
        self.store.set_meta(_CD_KEY.format(event=event_uid, user=user_id), str(now))
        self.store.set_meta(
            _HIST_KEY.format(event=event_uid, user=user_id),
            json.dumps(hist),
        )
