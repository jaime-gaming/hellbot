"""Leaderboard system — pure logic, no Discord imports.

Rules enforced here:
  * sorted from highest to lowest accumulated VC time;
  * the Top 3 are highlighted with medals, everyone else numbered below;
  * exact ties share the same rank (competition ranking: 1, 1, 3);
  * times render as `142h 38m`.

Filtering of bots / `@clanker` users and the "only while the event is active"
rule happen upstream in the tracker — a user simply never accumulates time in
those situations, so the leaderboard can stay a dumb, deterministic view.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .models import LeaderboardEntry, ParticipantRef
from .texts import TEXT, say
from .timeutil import format_hm


def build_leaderboard(rows: Iterable[tuple[int, str, float]]) -> list[LeaderboardEntry]:
    """Turn `(user_id, display_name, seconds)` rows into ranked entries.

    Sorting is deterministic: time desc, then name, then id — so two users with
    identical time always render in a stable order while sharing a rank.
    """
    ordered = sorted(rows, key=lambda r: (-round(r[2], 3), r[1].lower(), r[0]))

    entries: list[LeaderboardEntry] = []
    last_seconds: float | None = None
    last_rank = 0
    for index, (user_id, name, seconds) in enumerate(ordered, start=1):
        rounded = round(seconds, 3)
        if last_seconds is not None and rounded == last_seconds:
            rank = last_rank  # tie -> same rank
        else:
            rank = index
        entries.append(LeaderboardEntry(rank=rank, user_id=user_id, display_name=name, seconds=seconds))
        last_seconds = rounded
        last_rank = rank
    return entries


def top_n(entries: Sequence[LeaderboardEntry], n: int = 3) -> list[LeaderboardEntry]:
    """Entries whose rank is within the top `n` (tie-aware, so it may return more)."""
    return [e for e in entries if e.rank <= n]


def top_participants(entries: Sequence[LeaderboardEntry], n: int = 3) -> list[ParticipantRef]:
    return [ParticipantRef(e.user_id, e.display_name) for e in top_n(entries, n)]


def medals() -> dict[int, str]:
    """Podium markers, editable in Announcements.py."""
    return {int(k): v for k, v in dict(TEXT.LEADERBOARD_MEDALS).items()}


def format_entry(entry: LeaderboardEntry, use_mentions: bool = True) -> str:
    who = entry.mention() if use_mentions else entry.display_name
    prefix = medals().get(entry.rank, say(TEXT.LEADERBOARD_RANK, rank=entry.rank))
    return say(TEXT.LEADERBOARD_ENTRY, medal=prefix, who=who, time=format_hm(entry.seconds))


def render_leaderboard(
    entries: Sequence[LeaderboardEntry],
    *,
    title: str | None = None,
    limit: int | None = 25,
    use_mentions: bool = True,
    empty_note: str | None = None,
) -> str:
    """Render the leaderboard with the Top 3 visually separated from the rest."""
    title = title if title is not None else TEXT.LEADERBOARD_TITLE
    if not entries:
        return f"**{title}**\n{empty_note if empty_note is not None else TEXT.LEADERBOARD_EMPTY}"

    shown = list(entries[:limit]) if limit else list(entries)
    podium = [e for e in shown if e.rank <= 3]
    rest = [e for e in shown if e.rank > 3]

    lines = [f"**{title}**", ""]
    lines.extend(format_entry(e, use_mentions) for e in podium)
    if rest:
        lines.append("")
        lines.extend(format_entry(e, use_mentions) for e in rest)
    hidden = len(entries) - len(shown)
    if hidden > 0:
        lines.append("")
        lines.append(f"_…and {say(TEXT.LEADERBOARD_MORE, hidden=hidden)}._")
    return "\n".join(lines)
