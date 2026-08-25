"""Embed layout — how every message *looks*.

Split out of :mod:`hell.announcer` so that "what a message says" (this module
plus `Announcements.py`) stays separate from "how a message is delivered"
(channel resolution, editing the live progress message, retry/rate-limit
handling).  Nothing here touches the network or the database, so every layout
can be rendered and asserted on in a test.

All wording comes from `Announcements.py` via :mod:`hell.texts`; this module
only decides structure and enforces Discord's size limits.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Optional

import discord

from . import assets
from .config import Config
from .difficulty import DifficultyInfo
from .engine import (
    EventCancelled,
    EventCompleted,
    EventFailed,
    GraceRecovered,
    GraceStarted,
    MilestoneReached,
    Snapshot,
)
from .finale import FinaleAnnouncement
from .hellevents import HellEventEnded, HellEventStarted, is_secret_record
from .leaderboard import format_entry, format_entry_live, top_n
from .milestones import MILESTONES
from .models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from .texts import TEXT, say
from .timeutil import (
    discord_ts,
    format_hm,
    format_hms,
    milestone_bar,
    milestone_progress_bar,
)

log = logging.getLogger("hell.embeds")

# Discord's hard limits, with a safety margin kept everywhere.
MAX_CONTENT = 2000
MAX_DESCRIPTION = 4000      # hard limit 4096
MAX_FIELD = 1000            # hard limit 1024
MAX_EMBED_TOTAL = 5500      # hard limit 6000
MAX_FIELDS = 20             # hard limit 25
MAX_EMBEDS_PER_MESSAGE = 10

def theme_color(name: str, fallback: int = 0xE25822) -> int:
    """Look a colour up in Announcements.py (`COLOR_<NAME>`)."""
    try:
        return int(getattr(TEXT, f"COLOR_{name.upper()}", fallback))
    except (TypeError, ValueError):  # someone typed a colour name instead of a number
        log.warning("COLOR_%s in Announcements.py is not a number — using the default", name.upper())
        return fallback

def status_emoji(status: EventStatus) -> str:
    return dict(TEXT.STATUS_EMOJI).get(status.value, "🔥")

def status_color(status: EventStatus) -> int:
    return theme_color(status.value, theme_color("RUNNING"))

def bar(fraction: float, *, elapsed: Optional[float] = None) -> str:
    """The milestone-segmented progress bar, styled from Announcements.py.

    When ``elapsed`` is supplied (the running live message), each divided line
    fills progressively as *its* milestone approaches: completed milestones stay
    full, the current one fills on the way there, and locked ones stay empty.
    Without ``elapsed`` it falls back to a plain overall-progress bar.
    """
    cells_per_block = int(getattr(TEXT, "BAR_CELLS_PER_MILESTONE", 4))
    full = str(getattr(TEXT, "BAR_FULL", "▰"))
    empty = str(getattr(TEXT, "BAR_EMPTY", "▱"))
    separator = str(getattr(TEXT, "BAR_SEPARATOR", "┃"))
    if elapsed is not None and MILESTONES:
        return milestone_progress_bar(
            elapsed,
            [m.seconds for m in MILESTONES],
            cells_per_block=cells_per_block,
            full=full,
            empty=empty,
            separator=separator,
        )
    return milestone_bar(
        fraction,
        blocks=max(1, len(MILESTONES)),
        cells_per_block=cells_per_block,
        full=full,
        empty=empty,
        separator=separator,
    )

def dots(reached: int, total: Optional[int] = None) -> str:
    """Milestone tally, e.g. `🔥🔥🔥◦◦`."""
    total = len(MILESTONES) if total is None else total
    reached = max(0, min(reached, total))
    return str(getattr(TEXT, "DOT_REACHED", "🔥")) * reached + str(
        getattr(TEXT, "DOT_PENDING", "◦")
    ) * (total - reached)

def reached_count(elapsed: float) -> int:
    return sum(1 for m in MILESTONES if elapsed >= m.seconds)

def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters.

    Prefers line boundaries, falls back to `", "` boundaries (member lists),
    and hard-slices as a last resort so a single monstrous line can never
    produce an over-long payload.
    """
    if not text:
        return [""]

    def flush(buf: list[str], out: list[str]) -> None:
        if buf:
            out.append("\n".join(buf))

    out: list[str] = []
    buf: list[str] = []
    size = 0
    for raw_line in text.split("\n"):
        pieces = [raw_line]
        if len(raw_line) > limit:
            pieces = _split_line(raw_line, limit)
        for piece in pieces:
            piece_len = len(piece) + (1 if buf else 0)
            if size + piece_len > limit and buf:
                flush(buf, out)
                buf, size = [], 0
                piece_len = len(piece)
            buf.append(piece)
            size += piece_len
    flush(buf, out)
    return out or [""]

def _split_line(line: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for token in line.split(", "):
        candidate = token if not current else f"{current}, {token}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(token) > limit:  # pathological single token
            parts.append(token[:limit])
            token = token[limit:]
        current = token
    if current:
        parts.append(current)
    return parts

def add_chunked_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False) -> None:
    """Add a field, transparently splitting it across continuation fields."""
    chunks = split_text(value, MAX_FIELD)
    for index, chunk in enumerate(chunks):
        if len(embed.fields) >= MAX_FIELDS or len(embed) + len(chunk) + len(name) > MAX_EMBED_TOTAL:
            break
        embed.add_field(name=name if index == 0 else f"{name} (cont.)", value=chunk, inline=inline)

def embed_to_text(embed: discord.Embed) -> str:
    """Flatten an embed to plain text (used by tests and the offline simulator).

    Mirrors what a reader actually sees, author line included — otherwise the
    simulator and the tests would be blind to part of every message.
    """
    lines: list[str] = []
    if embed.author and embed.author.name:
        lines.append(str(embed.author.name))
    if embed.title:
        lines.append(f"**{embed.title}**")
    if embed.description:
        lines.append(embed.description)
    for field in embed.fields:
        lines.append("")
        lines.append(f"**{field.name}**")
        lines.append(str(field.value))
    if embed.footer and embed.footer.text:
        lines.append("")
        lines.append(f"_{embed.footer.text}_")
    return "\n".join(lines).strip()

def embeds_to_text(embeds: Sequence[discord.Embed]) -> str:
    return "\n\n".join(embed_to_text(e) for e in embeds)

def format_members(members: Sequence[ParticipantRef], *, empty: Optional[str] = None) -> str:
    if not members:
        return empty if empty is not None else TEXT.MILESTONE_NOBODY
    return ", ".join(m.mention() for m in members)

class EmbedFactory:
    """Builds every embed the bot posts.  Pure: no I/O, no state."""

    def __init__(self, config: Config):
        self.config = config

    # ------------------------------------------------------------- rewards

    def reward(self, milestone, *, short: bool = False) -> str:
        """Reward text with the role rendered as a real mention when possible."""
        text = (milestone.short_reward or milestone.reward) if short else milestone.reward
        token, env = milestone.role_token, milestone.role_env
        if not token or not env or token not in text:
            return text
        role_id = self.config.role_id(env)
        return text.replace(token, f"<@&{role_id}>") if role_id else text

    # ------------------------------------------------------------- styling

    def _brand(
        self,
        embed: discord.Embed,
        *,
        thumbnail: Optional[str] = None,
        image: Optional[str] = None,
        timestamp: bool = True,
    ) -> discord.Embed:
        """Apply the shared look: author line, artwork and a timestamp."""
        icon = assets.resolve(getattr(TEXT, "BRAND_ICON", ""))
        embed.set_author(name=str(getattr(TEXT, "BRAND_NAME", "Welcome to Hell")), icon_url=icon)
        thumb = assets.resolve(thumbnail)
        if thumb:
            embed.set_thumbnail(url=thumb)
        picture = assets.resolve(image)
        if picture:
            embed.set_image(url=picture)
        if timestamp:
            embed.timestamp = discord.utils.utcnow()
        return embed

    def progress(self, snap: Snapshot) -> discord.Embed:
        title = say(TEXT.PROGRESS_TITLE, emoji=status_emoji(snap.status))
        desc = say(
            TEXT.PROGRESS_DESCRIPTION,
            bar=bar(snap.fraction, elapsed=snap.elapsed),
            elapsed=format_hm(snap.elapsed),
            total=format_hm(snap.total),
            percent=f"{snap.fraction * 100:.1f}%",
            dots=dots(reached_count(snap.elapsed)),
        )
        if snap.status is EventStatus.RUNNING:
            if snap.continuation:
                title = str(getattr(TEXT, "PROGRESS_TITLE_CONTINUATION", "🔥 HELL 2 — KEEP ON HELL"))
                desc = (
                    f"`{bar(snap.fraction)}`\n"
                    f"**{format_hm(snap.elapsed)}** of {format_hm(snap.total)}  ·  **{snap.fraction * 100:.1f}%**\n\n"
                    f"🔒 **No milestones. One final and secret reward at {format_hm(snap.total)}.**"
                )
                if snap.remaining <= 60:
                    desc = (
                        f"`{bar(snap.fraction)}`\n"
                        f"**{format_hm(snap.elapsed)}** of {format_hm(snap.total)}  ·  **{snap.fraction * 100:.1f}%**\n\n"
                        f"👹 **FINAL COUNTDOWN: `{int(snap.remaining)}` SECONDS**\n"
                        f"**DO NOT LET HELL GO EMPTY.**"
                    )
            elif snap.countdown_seconds is not None:
                title = str(getattr(TEXT, "PROGRESS_TITLE_COUNTDOWN", "👹 FINAL COUNTDOWN"))
                desc = (
                    f"`{bar(snap.fraction, elapsed=snap.elapsed)}`\n"
                    f"**{format_hm(snap.elapsed)}** of {format_hm(snap.total)}  ·  **{snap.fraction * 100:.1f}%**\n\n"
                    f"👹 **FINAL COUNTDOWN: `{snap.countdown_seconds}` SECONDS REMAINING**\n"
                    f"**DO NOT LET HELL GO EMPTY.**"
                )
            elif snap.is_final_hour:
                title = str(getattr(TEXT, "PROGRESS_TITLE_FINAL_HOUR", "👹 THE FINAL HOUR"))
                desc = (
                    f"`{bar(snap.fraction, elapsed=snap.elapsed)}`\n"
                    f"**{format_hm(snap.elapsed)}** of {format_hm(snap.total)}  ·  **{snap.fraction * 100:.1f}%**  ·  {dots(reached_count(snap.elapsed))}\n\n"
                    f"👹 **1 HOUR REMAINING — DO NOT LET HELL GO EMPTY.**"
                )

        embed = discord.Embed(
            title=title,
            description=desc,
            color=status_color(snap.status),
        )
        self._brand(embed, thumbnail=getattr(TEXT, "PROGRESS_THUMBNAIL", ""))
        if snap.paused:
            template, status = TEXT.PROGRESS_STATUS_VALUE_PAUSED, "PAUSED"
        elif snap.grace_open:
            template, status = TEXT.PROGRESS_STATUS_VALUE_EMPTY_VC, snap.status.value
        else:
            template, status = TEXT.PROGRESS_STATUS_VALUE, snap.status.value
        embed.add_field(
            name=TEXT.PROGRESS_STATUS_FIELD,
            value=say(template, status=status),
            inline=True,
        )
        embed.add_field(
            name=TEXT.PROGRESS_PEOPLE_FIELD,
            value=say(TEXT.PROGRESS_PEOPLE_VALUE, participants=snap.participants),
            inline=True,
        )
        rem_val = (
            str(getattr(TEXT, "PROGRESS_REMAINING_BLIND", "👁️ *[HIDDEN BY BLINDNESS]*"))
            if snap.blindness_active and snap.status is EventStatus.RUNNING
            else say(TEXT.PROGRESS_REMAINING_VALUE, remaining=format_hm(snap.remaining))
        )
        embed.add_field(
            name=TEXT.PROGRESS_REMAINING_FIELD,
            value=rem_val,
            inline=True,
        )

        if snap.continuation:
            current = str(getattr(TEXT, "PROGRESS_CURRENT_CONTINUATION", "🔒 No milestones — only the final 320h reward"))
        else:
            current = (
                say(TEXT.PROGRESS_CURRENT_VALUE, current_milestone=snap.current.hours)
                if snap.current
                else TEXT.PROGRESS_CURRENT_NONE
            )
        embed.add_field(name=TEXT.PROGRESS_CURRENT_FIELD, value=current, inline=True)

        if snap.continuation:
            nxt = str(getattr(TEXT, "PROGRESS_NEXT_CONTINUATION", "🔒 Final secret reward at 320h"))
        elif snap.blindness_active and snap.status is EventStatus.RUNNING:
            nxt = str(getattr(TEXT, "PROGRESS_NEXT_BLIND", "👁️ *[HIDDEN BY BLINDNESS]*"))
        elif snap.upcoming and snap.time_to_next is not None:
            # While paused the live countdown tag is meaningless (the clock is
            # frozen), so fall back to the static "in X" rendering.
            running = snap.status is EventStatus.RUNNING and snap.start_ts and not snap.paused
            nxt = say(
                TEXT.PROGRESS_NEXT_VALUE_RUNNING if running else TEXT.PROGRESS_NEXT_VALUE,
                next_milestone=snap.upcoming.hours,
                time_to_next=format_hm(snap.time_to_next),
                next_relative=(
                    discord_ts((snap.start_ts or 0) + snap.upcoming.seconds, "R") if running else ""
                ),
            )
        else:
            nxt = TEXT.PROGRESS_NEXT_NONE
        embed.add_field(name=TEXT.PROGRESS_NEXT_FIELD, value=nxt, inline=True)

        if snap.start_ts:
            embed.add_field(
                name=TEXT.PROGRESS_STARTED_FIELD,
                value=say(
                    TEXT.PROGRESS_STARTED_VALUE,
                    started_at=discord_ts(snap.start_ts, "f"),
                    started_relative=discord_ts(snap.start_ts, "R"),
                ),
                inline=True,
            )

        if snap.status is EventStatus.FAILED:
            embed.add_field(
                name=TEXT.PROGRESS_FAILED_FIELD,
                value=snap.end_reason or TEXT.PROGRESS_FAILED_DEFAULT,
                inline=False,
            )
        elif snap.status is EventStatus.COMPLETED:
            value = (
                str(getattr(TEXT, "PROGRESS_COMPLETED_CONTINUATION_TEXT", "320H Hell 2 survived. The final secret reward is theirs."))
                if snap.continuation
                else TEXT.PROGRESS_COMPLETED_TEXT
            )
            embed.add_field(name=TEXT.PROGRESS_COMPLETED_FIELD, value=value, inline=False)
        elif snap.status is EventStatus.CANCELLED:
            embed.add_field(
                name=TEXT.PROGRESS_CANCELLED_FIELD,
                value=snap.end_reason or TEXT.PROGRESS_CANCELLED_DEFAULT,
                inline=False,
            )
        if snap.grace_open:
            embed.colour = discord.Colour(theme_color("GRACE"))
            embed.add_field(
                name=TEXT.PROGRESS_GRACE_FIELD,
                value=say(
                    TEXT.PROGRESS_GRACE_TEXT,
                    grace_left=f"{snap.grace_seconds_left:.0f}",
                    vc=f"<#{self.config.voice_channel_id}>",
                ),
                inline=False,
            )
        if snap.unverified >= 60:
            embed.add_field(
                name=TEXT.PROGRESS_UNVERIFIED_FIELD,
                value=say(TEXT.PROGRESS_UNVERIFIED_TEXT, unverified=format_hm(snap.unverified)),
                inline=False,
            )
        embed.set_footer(
            text=(
                say(TEXT.PROGRESS_FOOTER_LIVE, interval=int(self.config.progress_interval))
                if snap.status is EventStatus.RUNNING
                else TEXT.PROGRESS_FOOTER_FINAL
            )
        )
        return embed

    def start(
        self, snap: Snapshot, host_mention: str, participants: Sequence[ParticipantRef]
    ) -> discord.Embed:
        start_ts = snap.start_ts or 0
        fields = dict(
            host=host_mention,
            vc=f"<#{self.config.voice_channel_id}>",
            clanker_role=f"<@&{self.config.clanker_role_id}>",
            started_at=discord_ts(start_ts, "F"),
            started_relative=discord_ts(start_ts, "R"),
            ends_at=discord_ts(start_ts + snap.total, "F"),
            ends_relative=discord_ts(start_ts + snap.total, "R"),
            participant_count=len(participants),
            total_hours=int(snap.total // 3600),
            grace_seconds=int(self.config.empty_vc_grace_seconds),
        )
        embed = discord.Embed(
            title=say(TEXT.START_TITLE, **fields),
            description=say(TEXT.START_DESCRIPTION, **fields),
            color=theme_color("RUNNING"),
        )
        embed.add_field(
            name=say(TEXT.START_RULES_FIELD, **fields),
            value=say(TEXT.START_RULES, **fields),
            inline=False,
        )
        embed.add_field(
            name=say(TEXT.START_MILESTONES_FIELD, **fields),
            value="\n".join(
                say(
                    TEXT.START_MILESTONE_LINE,
                    hours=m.hours,
                    reward=self.reward(m),
                )
                for m in MILESTONES
            ),
            inline=False,
        )
        add_chunked_field(
            embed,
            say(TEXT.START_PARTICIPANTS_FIELD, **fields),
            format_members(participants, empty=TEXT.START_NOBODY),
        )
        embed.add_field(
            name=say(TEXT.START_FINISH_FIELD, **fields),
            value=say(TEXT.START_FINISH_TEXT, **fields),
            inline=False,
        )
        embed.set_footer(text=say(TEXT.START_FOOTER, **fields))
        self._brand(embed, thumbnail=getattr(TEXT, "PROGRESS_THUMBNAIL", ""))
        return embed

    def grace_warning(self, event: GraceStarted) -> discord.Embed:
        """Empty-VC warning.  Posted with pings explicitly disabled."""
        fields = dict(
            vc=f"<#{self.config.voice_channel_id}>",
            seconds=f"{event.seconds:.0f}",
            deadline_at=discord_ts(event.deadline_ts, "T"),
            deadline_relative=discord_ts(event.deadline_ts, "R"),
            elapsed=format_hm(event.elapsed),
        )
        embed = discord.Embed(
            title=say(TEXT.GRACE_WARNING_TITLE, **fields),
            description=say(TEXT.GRACE_WARNING_DESCRIPTION, **fields),
            color=theme_color("GRACE"),
        )
        embed.add_field(
            name=say(TEXT.GRACE_WARNING_DEADLINE_FIELD, **fields),
            value=say(TEXT.GRACE_WARNING_DEADLINE_TEXT, **fields),
            inline=True,
        )
        embed.add_field(
            name=say(TEXT.GRACE_WARNING_CLOCK_FIELD, **fields),
            value=say(TEXT.GRACE_WARNING_CLOCK_TEXT, **fields),
            inline=True,
        )
        embed.set_footer(text=say(TEXT.GRACE_WARNING_FOOTER, **fields))
        self._brand(embed)
        return embed

    def grace_recovered(self, event: GraceRecovered) -> discord.Embed:
        fields = dict(
            empty_for=f"{event.empty_for:.0f}",
            participant_count=len(event.participants),
            vc=f"<#{self.config.voice_channel_id}>",
        )
        embed = discord.Embed(
            title=say(TEXT.GRACE_RECOVERED_TITLE, **fields),
            description=say(TEXT.GRACE_RECOVERED_DESCRIPTION, **fields),
            color=theme_color("RUNNING"),
        )
        add_chunked_field(
            embed,
            say(TEXT.GRACE_RECOVERED_FIELD, **fields),
            format_members(event.participants, empty=TEXT.GRACE_RECOVERED_NOBODY),
        )
        embed.set_footer(text=say(TEXT.GRACE_RECOVERED_FOOTER, **fields))
        self._brand(embed)
        return embed

    def milestone(self, event: MilestoneReached, leaders: Sequence[LeaderboardEntry] = ()) -> discord.Embed:
        m = event.milestone
        fields = dict(
            hours=m.hours,
            remaining_hours=160 - m.hours,
            reward=self.reward(m),
            member_count=len(event.members),
            reached_at=discord_ts(event.reached_ts, "F"),
            reached_relative=discord_ts(event.reached_ts, "R"),
        )
        index = next((i for i, entry in enumerate(MILESTONES, 1) if entry.hours == m.hours), 1)
        tally = say(
            getattr(TEXT, "MILESTONE_TALLY", "{dots}"),
            dots=dots(index),
            index=index,
            count=len(MILESTONES),
        )
        description = f"{m.blurb}\n\n{tally}"
        if m.flavour:
            description = f"{m.blurb}\n\n*{m.flavour}*\n\n{tally}"
        embed = discord.Embed(
            title=m.title,
            description=description.strip(),
            color=theme_color("COMPLETED") if m.hours == 160 else theme_color("MILESTONE"),
        )
        embed.add_field(
            name=say(TEXT.MILESTONE_REWARD_FIELD, **fields),
            value=fields["reward"],
            inline=False,
        )
        embed.add_field(
            name=say(TEXT.MILESTONE_CLAIM_FIELD, **fields),
            value=say(TEXT.MILESTONE_CLAIM_TEXT, **fields),
            inline=False,
        )
        add_chunked_field(
            embed,
            say(TEXT.MILESTONE_ELIGIBLE_FIELD, **fields),
            format_members(event.members),
        )
        embed.add_field(
            name=say(TEXT.MILESTONE_REACHED_FIELD, **fields),
            value=say(TEXT.MILESTONE_REACHED_TEXT, **fields),
            inline=False,
        )
        podium = top_n(leaders, 3)
        if podium:
            add_chunked_field(
                embed,
                say(TEXT.MILESTONE_LEADERS_FIELD, **fields),
                "\n".join(format_entry(e) for e in podium),
            )
        if event.late:
            embed.add_field(
                name=say(TEXT.MILESTONE_LATE_FIELD, **fields),
                value=say(TEXT.MILESTONE_LATE_TEXT, **fields),
                inline=False,
            )
        footer = TEXT.MILESTONE_FOOTER_FINAL if m.hours == 160 else TEXT.MILESTONE_FOOTER
        embed.set_footer(text=say(footer, **fields))
        self._brand(embed, image=getattr(TEXT, "MILESTONE_IMAGE", ""))
        return embed

    def failure(self, event: EventFailed) -> list[discord.Embed]:
        reached = [m for m in MILESTONES if m.seconds <= event.elapsed]
        fields = dict(
            vc=f"<#{self.config.voice_channel_id}>",
            survived=format_hms(event.elapsed),
            percent=f"{event.elapsed / (160 * 3600) * 100:.1f}%",
            failed_at=discord_ts(event.failed_ts, "F"),
            milestones=(
                ", ".join(f"**{m.hours}h**" for m in reached)
                if reached
                else TEXT.FAILURE_MILESTONES_NONE
            ),
        )
        embed = discord.Embed(
            title=say(TEXT.FAILURE_TITLE, **fields),
            description=say(TEXT.FAILURE_DESCRIPTION, **fields),
            color=theme_color("FAILED"),
        )
        embed.add_field(
            name=say(TEXT.FAILURE_SURVIVED_FIELD, **fields),
            value=say(TEXT.FAILURE_SURVIVED_TEXT, **fields),
            inline=True,
        )
        embed.add_field(
            name=say(TEXT.FAILURE_PROGRESS_FIELD, **fields),
            value=say(TEXT.FAILURE_PROGRESS_TEXT, **fields),
            inline=True,
        )
        embed.add_field(
            name=say(TEXT.FAILURE_WHEN_FIELD, **fields), value=fields["failed_at"], inline=True
        )
        embed.add_field(
            name=say(TEXT.FAILURE_MILESTONES_FIELD, **fields),
            value=fields["milestones"],
            inline=False,
        )
        upcoming = next((m for m in MILESTONES if m.seconds > event.elapsed), None)
        if upcoming is not None:
            embed.add_field(
                name=say(TEXT.FAILURE_NEAR_MISS_FIELD, **fields),
                value=say(
                    TEXT.FAILURE_NEAR_MISS,
                    time_to_next=format_hm(upcoming.seconds - event.elapsed),
                    next_milestone=upcoming.hours,
                ),
                inline=False,
            )
        podium = top_n(event.leaderboard, 3)
        if podium:
            add_chunked_field(
                embed,
                say(TEXT.FAILURE_TOP_FIELD, **fields),
                "\n".join(format_entry(e) for e in podium),
            )
        embed.set_footer(text=say(TEXT.FAILURE_FOOTER, **fields))
        self._brand(embed)
        return [
            embed,
            *self.leaderboard(
                event.leaderboard,
                title=TEXT.FAILURE_LEADERBOARD_TITLE,
                color=theme_color("FAILED"),
            ),
        ]

    def cancelled(self, event: EventCancelled) -> list[discord.Embed]:
        fields = dict(
            who=f"<@{event.by_user_id}>" if event.by_user_id else "a host",
            elapsed=format_hms(event.elapsed),
            cancelled_at=discord_ts(event.cancelled_ts, "F"),
        )
        embed = discord.Embed(
            title=say(TEXT.CANCELLED_TITLE, **fields),
            description=say(TEXT.CANCELLED_DESCRIPTION, **fields),
            color=theme_color("CANCELLED"),
        )
        embed.add_field(
            name=say(TEXT.CANCELLED_CLOCK_FIELD, **fields),
            value=say(TEXT.CANCELLED_CLOCK_TEXT, **fields),
            inline=True,
        )
        embed.add_field(
            name=say(TEXT.CANCELLED_WHEN_FIELD, **fields), value=fields["cancelled_at"], inline=True
        )
        embed.set_footer(text=say(TEXT.CANCELLED_FOOTER, **fields))
        self._brand(embed)
        return [
            embed,
            *self.leaderboard(
                event.leaderboard,
                title=TEXT.CANCELLED_LEADERBOARD_TITLE,
                color=theme_color("CANCELLED"),
            ),
        ]

    def completion(self, event: EventCompleted) -> list[discord.Embed]:
        board = event.leaderboard
        podium = top_n(board, 3)
        continuation = bool(getattr(event, "continuation", False))
        if continuation:
            title = say(getattr(TEXT, "CONTINUATION_COMPLETION_TITLE", "🏆🔥 HELL 2 — 320H SURVIVED"), vc=f"<#{self.config.voice_channel_id}>")
            description = say(
                getattr(
                    TEXT,
                    "CONTINUATION_COMPLETION_DESCRIPTION",
                    "The full **320H** Hell 2 challenge has been survived. "
                    "The final, secret reward is now yours.",
                ),
                vc=f"<#{self.config.voice_channel_id}>",
                completed_at=discord_ts(event.completed_ts, "F"),
            )
            fields = dict(
                vc=f"<#{self.config.voice_channel_id}>",
                completed_at=discord_ts(event.completed_ts, "F"),
                final_reward=str(getattr(TEXT, "CONTINUATION_SECRET_REWARD", "🔒 *A final and secret reward*")),
                all_rewards="",
                bonus_role="",
            )
            reward_field = getattr(TEXT, "CONTINUATION_COMPLETION_REWARD_FIELD", "🎁 Final secret reward")
            reward_text = getattr(TEXT, "CONTINUATION_COMPLETION_REWARD_TEXT", "{final_reward}\n*Only those who survived to 320h.*")
        else:
            fields = dict(
                vc=f"<#{self.config.voice_channel_id}>",
                completed_at=discord_ts(event.completed_ts, "F"),
                final_reward=self.reward(MILESTONES[-1]),
                all_rewards=", ".join(
                    self.reward(m, short=True) for m in MILESTONES
                ),
                bonus_role=self.config.role_mention(
                    self.config.cool_people_role_id, TEXT.TOP3_BONUS_ROLE
                ),
            )
            title = say(getattr(TEXT, "COMPLETION_FINALE_TITLE", TEXT.COMPLETION_TITLE), **fields)
            description = say(getattr(TEXT, "COMPLETION_FINALE_DESCRIPTION", TEXT.COMPLETION_DESCRIPTION), **fields)
            reward_field = TEXT.COMPLETION_REWARD_FIELD
            reward_text = TEXT.COMPLETION_REWARD_TEXT

        embed = discord.Embed(
            title=title,
            description=description,
            color=theme_color("COMPLETED"),
        )
        embed.add_field(
            name=say(reward_field, **fields),
            value=say(reward_text, **fields),
            inline=False,
        )
        if not continuation:
            add_chunked_field(
                embed,
                say(TEXT.COMPLETION_TOP3_FIELD, **fields),
                say(TEXT.COMPLETION_TOP3_TEXT, **fields),
            )
            add_chunked_field(
                embed,
                say(TEXT.COMPLETION_PODIUM_FIELD, **fields),
                "\n".join(format_entry(e) for e in podium) or TEXT.COMPLETION_PODIUM_NONE,
            )
        if getattr(event, "peak_population", 0) > 0:
            embed.add_field(
                name="👥 Peak Hell Population",
                value=f"**{event.peak_population}** contestants inside simultaneously",
                inline=True,
            )
        footer = getattr(TEXT, "CONTINUATION_COMPLETION_FOOTER", "320h · no milestones · final secret reward") if continuation else TEXT.COMPLETION_FOOTER
        embed.set_footer(text=say(footer, **fields) if not continuation else footer)
        self._brand(embed, image=getattr(TEXT, "COMPLETION_IMAGE", ""))
        return [
            embed,
            *self.leaderboard(
                board,
                title=TEXT.COMPLETION_LEADERBOARD_TITLE,
                color=theme_color("COMPLETED"),
            ),
        ]

    def continuation_resume(self) -> discord.Embed:
        """Hell 2 start: 320h, no milestones, only a secret final reward."""
        fields = dict(
            hours=320,
            vc=f"<#{self.config.voice_channel_id}>",
        )
        embed = discord.Embed(
            title=say(getattr(TEXT, "CONTINUATION_RESUME_TITLE", "🔥 HELL 2 — THE KEEP ON HELL CHALLENGE"), **fields),
            description=say(
                getattr(
                    TEXT,
                    "CONTINUATION_RESUME_DESCRIPTION",
                    "Hell is not over. The same run now continues to **320 hours**. "
                    "There are no more milestones — only one final and secret reward.",
                ),
                **fields,
            ),
            color=theme_color("CONTINUATION", theme_color("RUNNING")),
        )
        embed.add_field(
            name=getattr(TEXT, "CONTINUATION_RESUME_RULE_FIELD", "📜 The rule"),
            value=getattr(
                TEXT,
                "CONTINUATION_RESUME_RULE",
                "• Keep at least one real human in **{vc}** until **320h**.\n"
                "• No milestones will be announced after 160h.\n"
                "• At **320h**, a final and secret reward awaits the survivors.",
            ).format(**fields),
            inline=False,
        )
        embed.set_footer(text=getattr(TEXT, "CONTINUATION_RESUME_FOOTER", "320h · no milestones · secret reward"))
        self._brand(embed, thumbnail=getattr(TEXT, "PROGRESS_THUMBNAIL", ""))
        return embed

    def hell_event_start(self, event: HellEventStarted) -> discord.Embed:
        rec = event.record
        secret = bool(getattr(event, "secret", False)) or is_secret_record(rec)
        if secret:
            # Say that *something* happened — but never what.
            embed = discord.Embed(
                title=str(
                    getattr(TEXT, "HELL_EVENT_SECRET_TITLE", "🕯️ SECRET HELL EVENT — ???")
                ),
                description=event.announcement_text,
                color=theme_color("RUNNING"),
            )
            embed.set_footer(
                text=str(
                    getattr(
                        TEXT,
                        "HELL_EVENT_SECRET_FOOTER",
                        "Its nature stays hidden until it ends",
                    )
                )
            )
            self._brand(embed, timestamp=True)
            return embed

        embed = discord.Embed(
            title=say(
                getattr(TEXT, "HELL_EVENT_TITLE", "⚡ HELL EVENT — {name}"),
                name=rec.name.upper(),
            ),
            description=event.announcement_text,
            color=theme_color("RUNNING"),
        )
        if rec.end_ts > rec.start_ts:
            embed.add_field(
                name="⏳ Duration",
                value=f"**{int((rec.end_ts - rec.start_ts) // 60)} minutes** (ends <t:{int(rec.end_ts)}:R>)",
                inline=True,
            )
        if event.eligible_participants:
            add_chunked_field(
                embed,
                f"👥 Eligible ({len(event.eligible_participants)})",
                format_members(event.eligible_participants),
            )
        embed.set_footer(text="Hell Event active · Dynamic survival modifiers applied")
        self._brand(embed, timestamp=True)
        return embed

    def hell_event_end(self, event: HellEventEnded) -> discord.Embed:
        rec = event.record
        if is_secret_record(rec):
            # The veil lifts: this is the moment a secret event is identified.
            embed = discord.Embed(
                title=say(
                    getattr(
                        TEXT,
                        "HELL_EVENT_SECRET_END_TITLE",
                        "🕯️ SECRET HELL EVENT REVEALED — {name}",
                    ),
                    name=rec.name.upper(),
                ),
                description=event.announcement_text,
                color=theme_color("RUNNING"),
            )
            embed.set_footer(text="The secret is out · Modifiers returned to normal · Keep surviving")
            self._brand(embed, timestamp=True)
            return embed

        embed = discord.Embed(
            title=f"⚡ HELL EVENT CONCLUDED — {rec.name.upper()}",
            description=event.announcement_text,
            color=theme_color("RUNNING"),
        )
        embed.set_footer(text="Modifiers returned to normal · Keep surviving")
        self._brand(embed, timestamp=True)
        return embed

    def finale_stage(self, ann: FinaleAnnouncement) -> discord.Embed:
        embed = discord.Embed(
            title=ann.title,
            description=ann.description,
            color=theme_color("RUNNING"),
        )
        embed.set_footer(text="The 160-Hour Finale · Keep the voice channel alive!")
        self._brand(embed, timestamp=True)
        return embed

    def leaderboard(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        title: Optional[str] = None,
        color: Optional[int] = None,
        limit: int = 50,
    ) -> list[discord.Embed]:
        """Podium + the rest, split across as many embeds as needed."""
        title = title if title is not None else TEXT.LEADERBOARD_TITLE
        colour = color if color is not None else theme_color("RUNNING")

        if not entries:
            return [discord.Embed(title=title, description=TEXT.LEADERBOARD_EMPTY, color=colour)]

        shown = list(entries[:limit])
        podium = [e for e in shown if e.rank <= 3]
        rest = [e for e in shown if e.rank > 3]

        head = discord.Embed(
            title=title,
            description="\n".join(format_entry(e) for e in podium) or TEXT.LEADERBOARD_NO_PODIUM,
            color=colour,
        )
        total = len(entries)
        head.set_footer(
            text=say(
                TEXT.LEADERBOARD_FOOTER,
                total=total,
                tracked=format_hm(sum(e.seconds for e in entries)),
            )
        )
        self._brand(head, timestamp=False)
        embeds = [head]
        if rest:
            body = "\n".join(format_entry(e) for e in rest)
            for index, chunk in enumerate(split_text(body, MAX_DESCRIPTION)):
                embeds.append(
                    discord.Embed(
                        title=(
                            TEXT.LEADERBOARD_REST_TITLE
                            if index == 0
                            else TEXT.LEADERBOARD_REST_TITLE_CONT
                        ),
                        description=chunk,
                        color=colour,
                    )
                )
        hidden = total - len(shown)
        if hidden > 0:
            embeds[-1].add_field(
                name="…", value=say(TEXT.LEADERBOARD_MORE, hidden=hidden), inline=False
            )
        return embeds[:MAX_EMBEDS_PER_MESSAGE]

    def leaderboard_live(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        title: Optional[str] = None,
        color: Optional[int] = None,
        limit: int = 50,
        top_n: int = 5,
    ) -> list[discord.Embed]:
        """Live leaderboard: Top *top_n* show time with seconds, rest show position only."""
        title = title if title is not None else TEXT.LEADERBOARD_TITLE
        colour = color if color is not None else theme_color("RUNNING")

        if not entries:
            return [discord.Embed(title=title, description=TEXT.LEADERBOARD_EMPTY, color=colour)]

        shown = list(entries[:limit])
        podium = [e for e in shown if e.rank <= top_n]
        rest = [e for e in shown if e.rank > top_n]

        head = discord.Embed(
            title=title,
            description="\n".join(format_entry_live(e, top_n=top_n) for e in podium) or TEXT.LEADERBOARD_NO_PODIUM,
            color=colour,
        )
        total = len(entries)
        head.set_footer(
            text=say(
                TEXT.LEADERBOARD_FOOTER,
                total=total,
                tracked=format_hm(sum(e.seconds for e in entries)),
            )
        )
        self._brand(head, timestamp=False)
        embeds = [head]
        if rest:
            body = "\n".join(format_entry_live(e, top_n=top_n) for e in rest)
            for index, chunk in enumerate(split_text(body, MAX_DESCRIPTION)):
                embeds.append(
                    discord.Embed(
                        title=(
                            TEXT.LEADERBOARD_REST_TITLE
                            if index == 0
                            else TEXT.LEADERBOARD_REST_TITLE_CONT
                        ),
                        description=chunk,
                        color=colour,
                    )
                )
        hidden = total - len(shown)
        if hidden > 0:
            embeds[-1].add_field(
                name="…", value=say(TEXT.LEADERBOARD_MORE, hidden=hidden), inline=False
            )
        return embeds[:MAX_EMBEDS_PER_MESSAGE]

    def status(
        self,
        snap: Snapshot,
        *,
        milestone_records: Sequence[MilestoneRecord] = (),
        alive_line: Optional[str] = None,
    ) -> discord.Embed:
        embed = self.progress(snap)
        from .difficulty import get_difficulty
        diff = get_difficulty(snap.elapsed)
        embed.add_field(
            name=str(getattr(TEXT, "DIFFICULTY_STATUS_FIELD", "⚡ Difficulty")),
            value=say(
                str(getattr(TEXT, "DIFFICULTY_STATUS_VALUE", "**Level {level}** — {name}")),
                level=diff.level,
                name=diff.name,
            ) + f"\n*{diff.description}*",
            inline=False,
        )
        if alive_line:
            embed.add_field(name="🚨 Alive checks", value=alive_line, inline=False)
        records = list(milestone_records)
        if records:
            add_chunked_field(
                embed,
                "🏁 Milestones reached",
                "\n".join(
                    f"**{r.hours}h** — {discord_ts(r.reached_ts, 'f')} — "
                    f"{len(r.members)} eligible user(s)"
                    for r in records
                ),
            )
        return embed

    def difficulty_info(self, elapsed: float, override: Optional[int] = None) -> discord.Embed:
        """Overview of the 5 difficulty tiers and current state."""
        from .difficulty import DIFFICULTIES, get_difficulty

        current = get_difficulty(elapsed, override=override)
        embed = discord.Embed(
            title=str(getattr(TEXT, "CMD_DIFFICULTY_TITLE", "⚡ WELCOME TO HELL — DIFFICULTIES")),
            description=str(getattr(TEXT, "CMD_DIFFICULTY_DESCRIPTION", "Difficulties make the challenge progressively harder at each milestone reached.")),
            color=theme_color("RUNNING"),
        )
        embed.add_field(
            name="⚡ Current Difficulty",
            value=f"**Level {current.level}** — **{current.name}**\n*{current.description}*",
            inline=False,
        )
        for lvl in range(5):
            d = DIFFICULTIES[lvl]
            unlocked = "✅ Active" if current.level >= lvl else f"🔒 Unlocks at {d.unlock_hours}h"
            active_marker = " 👈 (CURRENT)" if current.level == lvl else ""
            dead_info = "Disabled"
            if d.dead_checks_enabled:
                if d.min_mute_seconds == d.max_mute_seconds:
                    dead_info = f"Enabled ({d.min_mute_seconds // 60}m mute on reply)"
                else:
                    dead_info = f"Enabled ({d.min_mute_seconds // 60}–{d.max_mute_seconds // 60}m mute on reply)"
            gamble_info = "Locked"
            if d.gamble_enabled:
                gamble_info = (
                    f"Active (`/hell gamble [hours]` / `!gamble [hours]`) — "
                    f"Win: **+{d.gamble_win_multiplier:g}x** ({int(d.gamble_win_chance * 100)}% odds), "
                    f"Lose: **-1.0x** + {d.gamble_loss_mute_seconds // 60}m mute | "
                    f"Limits: max **{d.gamble_max_bet_hours:g}h** bet, max **{d.gamble_hourly_limit}/hour**"
                )
            embed.add_field(
                name=f"Level {d.level}: {d.name} — {unlocked}{active_marker}",
                value=f"• **Alive checks:** every {d.min_check_hours:g}–{d.max_check_hours:g}h\n"
                      f"• **Dead checks:** {dead_info}\n"
                      f"• **Gambling:** {gamble_info}",
                inline=False,
            )
        self._brand(embed, timestamp=False)
        return embed

    def difficulty_announcement(self, diff: DifficultyInfo) -> discord.Embed:
        """Announcement embed for a difficulty change/unlock."""
        dead_info = "Disabled"
        if diff.dead_checks_enabled:
            if diff.min_mute_seconds == diff.max_mute_seconds:
                dead_info = f"Enabled ({diff.min_mute_seconds // 60}m mute on reply)"
            else:
                dead_info = f"Enabled ({diff.min_mute_seconds // 60}–{diff.max_mute_seconds // 60}m mute on reply)"
        gamble_info = "Locked"
        if diff.gamble_enabled:
            gamble_info = (
                f"Active (`/hell gamble [hours]` / `!gamble [hours]`) — "
                f"Win: **+{diff.gamble_win_multiplier:g}x** ({int(diff.gamble_win_chance * 100)}% odds), "
                f"Lose: **-1.0x** + {diff.gamble_loss_mute_seconds // 60}m mute | "
                f"Limits: max **{diff.gamble_max_bet_hours:g}h** bet, max **{diff.gamble_hourly_limit}/hour**"
            )

        embed = discord.Embed(
            title=say(
                getattr(TEXT, "DIFFICULTY_ANNOUNCE_TITLE", "⚡ DIFFICULTY UPDATE — LEVEL {level} ({name})"),
                level=diff.level,
                name=diff.name,
            ),
            description=(
                f"**Welcome to Hell is now at Difficulty Level {diff.level}: {diff.name}!** 🔥\n\n"
                f"*{diff.description}*\n\n"
                f"• **Alive checks:** every **{diff.min_check_hours:g}–{diff.max_check_hours:g}h**\n"
                f"• **Dead checks:** **{dead_info}**\n"
                f"• **Gambling:** **{gamble_info}**"
            ),
            color=theme_color("RUNNING"),
        )
        embed.set_footer(text="Survive the challenge · Use /hell difficulty or /hell gamble")
        self._brand(embed, timestamp=True)
        return embed
