"""Time helpers.

Every timestamp in the bot is an absolute POSIX timestamp (UTC, seconds as
float).  Nothing anywhere relies on process uptime, so restarting the bot can
never reset or shift the event timer.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ts() -> float:
    """Current absolute UTC timestamp (seconds)."""
    return time.time()


def to_dt(ts: float) -> datetime:
    """Convert a POSIX timestamp into an aware UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def discord_ts(ts: float, style: str = "f") -> str:
    """Render a Discord dynamic timestamp tag (renders in viewer local time)."""
    return f"<t:{int(ts)}:{style}>"


def format_hm(seconds: float) -> str:
    """`142h 38m` — the leaderboard / progress format.

    Negative values clamp to zero; minutes are zero padded to two digits.
    """
    seconds = max(0.0, float(seconds))
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_hms(seconds: float) -> str:
    """`142h 38m 12s` — used where second precision matters."""
    seconds = max(0.0, float(seconds))
    total_seconds = int(seconds)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def progress_bar(fraction: float, width: int = 20, full: str = "█", empty: str = "░") -> str:
    """Visual progress bar, e.g. `██████████░░░░░░░░░░`."""
    fraction = min(1.0, max(0.0, float(fraction)))
    filled = int(round(fraction * width))
    # Never show a completely full bar unless we truly are at 100%.
    if filled >= width and fraction < 1.0:
        filled = width - 1
    # Show at least one block once any progress exists.
    if filled == 0 and fraction > 0:
        filled = 1
    return full * filled + empty * (width - filled)
