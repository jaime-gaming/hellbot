"""Time helpers.

Every timestamp in the bot is an absolute POSIX timestamp (UTC, seconds as
float).  Nothing anywhere relies on process uptime, so restarting the bot can
never reset or shift the event timer.
"""

from __future__ import annotations

import time
from collections.abc import Sequence


def now_ts() -> float:
    """Current absolute UTC timestamp (seconds)."""
    return time.time()


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


def format_clock(seconds: float) -> str:
    """`0:00:00` — hours:minutes:seconds, hours unbounded (stat cards)."""
    seconds = max(0.0, float(seconds))
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def milestone_bar(
    fraction: float,
    *,
    blocks: int = 5,
    cells_per_block: int = 4,
    full: str = "▰",
    empty: str = "▱",
    separator: str = "┃",
) -> str:
    """A progress bar split into one segment per milestone.

    `▰▰▰▰┃▰▰▱▱┃▱▱▱▱┃▱▱▱▱┃▱▱▱▱` — at a glance you can see both the overall
    progress and which milestone block the event is currently inside.
    """
    total_cells = max(1, blocks * cells_per_block)
    fraction = min(1.0, max(0.0, float(fraction)))
    filled = round(fraction * total_cells)
    if filled >= total_cells and fraction < 1.0:
        filled = total_cells - 1          # never look finished early
    if filled == 0 and fraction > 0:
        filled = 1                        # …or empty once it has started

    cells = [full if i < filled else empty for i in range(total_cells)]
    segments = [
        "".join(cells[i : i + cells_per_block]) for i in range(0, total_cells, cells_per_block)
    ]
    return separator.join(segments)


def milestone_progress_bar(
    elapsed: float,
    milestones: Sequence[float],
    *,
    cells_per_block: int = 4,
    full: str = "▰",
    empty: str = "▱",
    separator: str = "┃",
) -> str:
    """A bar split into one segment per milestone, filled block-by-block.

    Each `┃`-divided line represents a milestone.  Completed milestones stay
    full, the milestone currently being approached fills gradually as the
    event clock gets closer, and future milestones stay empty.  The current
    segment never shows a full block until the milestone is actually reached.
    """
    milestones = [float(m) for m in milestones]
    elapsed = max(0.0, float(elapsed))
    segments: list[str] = []
    for index, end in enumerate(milestones):
        start = milestones[index - 1] if index else 0.0
        if elapsed >= end:
            filled = cells_per_block
        elif elapsed > start:
            span = max(1e-9, end - start)
            local = min(1.0, max(0.0, (elapsed - start) / span))
            filled = round(local * cells_per_block)
            if filled == 0:
                filled = 1                     # show it has started
            filled = max(0, min(cells_per_block - 1, filled))  # never full early
        else:
            filled = 0
        segments.append(full * filled + empty * (cells_per_block - filled))
    return separator.join(segments)


def progress_bar(fraction: float, width: int = 20, full: str = "█", empty: str = "░") -> str:
    """Visual progress bar, e.g. `██████████░░░░░░░░░░`."""
    fraction = min(1.0, max(0.0, float(fraction)))
    filled = round(fraction * width)
    # Never show a completely full bar unless we truly are at 100%.
    if filled >= width and fraction < 1.0:
        filled = width - 1
    # Show at least one block once any progress exists.
    if filled == 0 and fraction > 0:
        filled = 1
    return full * filled + empty * (width - filled)
