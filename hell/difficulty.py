"""Difficulties system — makes the challenge progressively harder at each milestone.

There are 5 difficulty levels (0 to 4):
- 0: Starter (0h - 32h)
     Alive checks every 1-6h. No dead checks. No gambling.
- 1: Unlocked at 32h milestone (32h - 64h)
     Alive checks every 1-5h. No dead checks. No gambling.
- 2: Unlocked at 64h milestone (64h - 96h)
     Alive checks every 1-4h. Dead checks enabled (saying yes = 1 min mute).
- 3: Unlocked at 96h milestone (96h - 128h)
     Alive checks every 1-3h. Dead checks (1-5 min mute).
     Timer gambling unlocked (max 1h bet, max 2 bets/hr, 40% win rate, +1.5x multiplier).
- 4: Unlocked at 128h milestone (128h - 160h)
     Alive checks every 1-2h. Dead checks (5-15 min mute).
     High-stakes gambling (max 2h bet, max 3 bets/hr, 30% win rate, +2.5x multiplier).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DifficultyInfo:
    """Attributes and rules for a specific difficulty level."""

    level: int
    name: str
    unlock_hours: int
    min_check_hours: float
    max_check_hours: float
    dead_checks_enabled: bool
    dead_check_chance: float
    min_mute_seconds: int
    max_mute_seconds: int
    gamble_enabled: bool
    gamble_win_chance: float
    gamble_max_bet_hours: float
    gamble_hourly_limit: int
    gamble_win_multiplier: float
    gamble_loss_mute_seconds: int
    gamble_cooldown_seconds: float
    gamble_overflow_cooldown_seconds: float
    description: str

    @property
    def unlock_seconds(self) -> float:
        return float(self.unlock_hours * 3600)


DIFFICULTIES: dict[int, DifficultyInfo] = {
    0: DifficultyInfo(
        level=0,
        name="Starter",
        unlock_hours=0,
        min_check_hours=1.0,
        max_check_hours=6.0,
        dead_checks_enabled=False,
        dead_check_chance=0.0,
        min_mute_seconds=0,
        max_mute_seconds=0,
        gamble_enabled=False,
        gamble_win_chance=0.0,
        gamble_max_bet_hours=0.0,
        gamble_hourly_limit=0,
        gamble_win_multiplier=0.0,
        gamble_loss_mute_seconds=0,
        gamble_cooldown_seconds=0.0,
        gamble_overflow_cooldown_seconds=0.0,
        description="Starter difficulty: alive checks every 1–6h. No dead checks or gambling.",
    ),
    1: DifficultyInfo(
        level=1,
        name="Heating Up",
        unlock_hours=32,
        min_check_hours=1.0,
        max_check_hours=5.0,
        dead_checks_enabled=False,
        dead_check_chance=0.0,
        min_mute_seconds=0,
        max_mute_seconds=0,
        gamble_enabled=False,
        gamble_win_chance=0.0,
        gamble_max_bet_hours=0.0,
        gamble_hourly_limit=0,
        gamble_win_multiplier=0.0,
        gamble_loss_mute_seconds=0,
        gamble_cooldown_seconds=0.0,
        gamble_overflow_cooldown_seconds=0.0,
        description="Unlocked at 32h milestone: alive checks every 1–5h.",
    ),
    2: DifficultyInfo(
        level=2,
        name="Inferno",
        unlock_hours=64,
        min_check_hours=1.0,
        max_check_hours=4.0,
        dead_checks_enabled=True,
        dead_check_chance=0.25,
        min_mute_seconds=60,
        max_mute_seconds=60,
        gamble_enabled=False,
        gamble_win_chance=0.0,
        gamble_max_bet_hours=0.0,
        gamble_hourly_limit=0,
        gamble_win_multiplier=0.0,
        gamble_loss_mute_seconds=0,
        gamble_cooldown_seconds=0.0,
        gamble_overflow_cooldown_seconds=0.0,
        description="Unlocked at 64h milestone: alive checks every 1–4h, dead checks active (saying yes = 1 min mute).",
    ),
    3: DifficultyInfo(
        level=3,
        name="Torment",
        unlock_hours=96,
        min_check_hours=1.0,
        max_check_hours=3.0,
        dead_checks_enabled=True,
        dead_check_chance=0.40,
        min_mute_seconds=60,
        max_mute_seconds=300,
        gamble_enabled=True,
        gamble_win_chance=0.40,            # 40% win chance
        gamble_max_bet_hours=1.0,          # Max 1 hour bet
        gamble_hourly_limit=2,             # 2 fast bets, then overflow timer
        gamble_win_multiplier=1.5,         # Win +1.5x bet
        gamble_loss_mute_seconds=60,       # 1 minute mute
        gamble_cooldown_seconds=300.0,     # 5 minute gap between fast Real Timer bets
        gamble_overflow_cooldown_seconds=2700.0,  # 45m between extra Real Timer gambles
        description="Unlocked at 96h milestone: alive checks every 1–3h, dead checks (1–5 min mute), timer gambling (max 1h bet, 2 fast Real Timer bets/h then 45m timer, 40% win).",
    ),
    4: DifficultyInfo(
        level=4,
        name="Cataclysm",
        unlock_hours=128,
        min_check_hours=1.0,
        max_check_hours=2.0,
        dead_checks_enabled=True,
        dead_check_chance=0.55,
        min_mute_seconds=300,
        max_mute_seconds=900,
        gamble_enabled=True,
        gamble_win_chance=0.30,            # 30% win chance (harder)
        gamble_max_bet_hours=2.0,          # Max 2 hours bet
        gamble_hourly_limit=3,             # 3 fast bets, then overflow timer
        gamble_win_multiplier=2.5,         # Win +2.5x bet
        gamble_loss_mute_seconds=60,        # 1 minute mute
        gamble_cooldown_seconds=240.0,      # 4 minute gap between fast Real Timer bets
        gamble_overflow_cooldown_seconds=1800.0,  # 30m between extra Real Timer gambles
        description="Unlocked at 128h milestone: alive checks every 1–2h, dead checks (5–15 min mute), high-stakes gambling (max 2h bet, 3 fast Real Timer bets/h then 30m timer, 30% win).",
    ),
}


def get_difficulty_by_level(level: int) -> DifficultyInfo:
    """Fetch difficulty definition by level integer (0-4)."""
    lvl = max(0, min(4, int(level)))
    return DIFFICULTIES[lvl]


def get_difficulty(elapsed_seconds: float, override: Optional[int] = None) -> DifficultyInfo:
    """Compute the current difficulty based on elapsed event seconds or explicit override."""
    if override is not None:
        return get_difficulty_by_level(override)
    hours = elapsed_seconds / 3600.0
    if hours >= 128.0:
        return DIFFICULTIES[4]
    if hours >= 96.0:
        return DIFFICULTIES[3]
    if hours >= 64.0:
        return DIFFICULTIES[2]
    if hours >= 32.0:
        return DIFFICULTIES[1]
    return DIFFICULTIES[0]


def next_difficulty(elapsed_seconds: float, override: Optional[int] = None) -> Optional[DifficultyInfo]:
    """Get the next upcoming difficulty tier, or None if at maximum."""
    current = get_difficulty(elapsed_seconds, override=override)
    if current.level >= 4:
        return None
    return DIFFICULTIES[current.level + 1]
