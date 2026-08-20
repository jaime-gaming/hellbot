"""Reward / announcement system — every message the bot posts.

Design notes
------------
* Messages are **embeds**. They give 4096 characters of description (versus
  2000 for plain content), render far better, and — importantly — mentions
  inside an embed never ping anyone. So a milestone can list 200 eligible
  users without spamming 200 notifications, while the `@everyone` ping stays
  in the message content where it belongs.
* Every piece of text is length-guarded (`split_text`, `add_chunked_field`).
  A previous version could produce a single 2000+ character line when many
  users were in the VC, which Discord rejects with HTTP 400 — that class of
  bug is now impossible.
* Discord failures are logged and swallowed: a message must never take the
  event timer down with it.  Milestones are only flagged as announced once
  Discord actually accepted the message.
* **No wording lives in this file.**  Every string, colour and emoji comes from
  `Announcements.py` through :mod:`hell.texts`, so the event's voice can be
  rewritten without touching any logic (and reloaded live with
  `/hell reloadmessages`).  This module only decides *which* message to build
  and how to keep it inside Discord's limits.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import discord

from .config import Config
from .engine import (
    EventCancelled,
    EventCompleted,
    EventFailed,
    GraceRecovered,
    GraceStarted,
    HellEngine,
    MilestoneReached,
    Snapshot,
)
from .leaderboard import format_entry, render_leaderboard, top_n
from .milestones import MILESTONES, get_milestone
from .models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from .texts import TEXT, say
from .timeutil import discord_ts, format_hm, format_hms, progress_bar

log = logging.getLogger("hell.announcer")

MAX_CONTENT = 2000
MAX_DESCRIPTION = 4000      # 4096 hard limit, margin kept
MAX_FIELD = 1000            # 1024 hard limit, margin kept
MAX_EMBED_TOTAL = 5500      # 6000 hard limit, margin kept
MAX_FIELDS = 20             # 25 hard limit, margin kept
MAX_EMBEDS_PER_MESSAGE = 10


# Colours and status emojis live in Announcements.py too, so the whole look of
# the bot can be tuned from that one file.


def color(name: str, fallback: int = 0xE25822) -> int:
    return int(getattr(TEXT, f"COLOR_{name.upper()}", fallback))


def status_emoji(status: EventStatus) -> str:
    return dict(TEXT.STATUS_EMOJI).get(status.value, "🔥")


def status_color(status: EventStatus) -> int:
    return color(status.value, color("RUNNING"))

# --------------------------------------------------------------- text tools


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


def chunk_lines(lines: Sequence[str], limit: int = MAX_CONTENT - 100) -> list[str]:
    """Backwards-compatible helper: chunk a list of lines into safe messages."""
    return split_text("\n".join(lines), limit)


def add_chunked_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False) -> None:
    """Add a field, transparently splitting it across continuation fields."""
    chunks = split_text(value, MAX_FIELD)
    for index, chunk in enumerate(chunks):
        if len(embed.fields) >= MAX_FIELDS or len(embed) + len(chunk) + len(name) > MAX_EMBED_TOTAL:
            break
        embed.add_field(name=name if index == 0 else f"{name} (cont.)", value=chunk, inline=inline)


def embed_to_text(embed: discord.Embed) -> str:
    """Flatten an embed to plain text (used by tests and the offline simulator)."""
    lines: list[str] = []
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


# ------------------------------------------------------------------ announcer


class Announcer:
    """Owns the announcement channel and the single live progress message."""

    def __init__(self, bot: discord.Client, config: Config, engine: HellEngine):
        self.bot = bot
        self.config = config
        self.engine = engine
        self._progress_message: Optional[discord.Message] = None
        self._last_progress_payload: Optional[str] = None

    # ----------------------------------------------------------- plumbing

    async def channel(self) -> Optional[discord.abc.Messageable]:
        cid = self.engine.state.announce_channel_id or self.config.announce_channel_id
        chan = self.bot.get_channel(cid)
        if chan is None:
            try:
                chan = await self.bot.fetch_channel(cid)
            except discord.HTTPException as exc:
                log.error("Announcement channel %s unreachable: %s", cid, exc)
                return None
        if not isinstance(chan, discord.abc.Messageable):
            log.error("Configured announcement channel %s is not a text channel", cid)
            return None
        return chan

    async def send(
        self,
        embeds: Sequence[discord.Embed],
        *,
        content: Optional[str] = None,
        mention_everyone: bool = False,
    ) -> Optional[discord.Message]:
        """Post one or more embeds, chunked into as many messages as needed."""
        chan = await self.channel()
        if chan is None:
            return None
        allowed = discord.AllowedMentions(
            everyone=mention_everyone, users=False, roles=False, replied_user=False
        )
        first: Optional[discord.Message] = None
        batch = list(embeds) or []
        try:
            for index in range(0, max(1, len(batch)), MAX_EMBEDS_PER_MESSAGE):
                slice_ = batch[index : index + MAX_EMBEDS_PER_MESSAGE]
                msg = await chan.send(
                    content=(content if index == 0 else None),
                    embeds=slice_,
                    allowed_mentions=allowed,
                )
                first = first or msg
        except discord.Forbidden:
            log.error(
                "Missing permission to post in the announcement channel (%s). "
                "Grant View Channel / Send Messages / Embed Links%s.",
                self.config.announce_channel_id,
                " / Mention @everyone" if mention_everyone else "",
            )
        except discord.HTTPException as exc:
            log.error("Failed to send announcement: %s", exc)
        return first

    # ------------------------------------------------------------- progress

    def build_progress(self, snap: Snapshot) -> discord.Embed:
        embed = discord.Embed(
            title=say(TEXT.PROGRESS_TITLE, emoji=status_emoji(snap.status)),
            description=say(
                TEXT.PROGRESS_DESCRIPTION,
                bar=progress_bar(snap.fraction),
                elapsed=format_hm(snap.elapsed),
                total=format_hm(snap.total),
                percent=f"{snap.fraction * 100:.1f}%",
            ),
            color=status_color(snap.status),
        )
        template = (
            TEXT.PROGRESS_STATUS_VALUE_EMPTY_VC if snap.grace_open else TEXT.PROGRESS_STATUS_VALUE
        )
        embed.add_field(
            name=TEXT.PROGRESS_STATUS_FIELD,
            value=say(template, status=snap.status.value),
            inline=True,
        )
        embed.add_field(
            name=TEXT.PROGRESS_PEOPLE_FIELD,
            value=say(TEXT.PROGRESS_PEOPLE_VALUE, participants=snap.participants),
            inline=True,
        )
        embed.add_field(
            name=TEXT.PROGRESS_REMAINING_FIELD,
            value=say(TEXT.PROGRESS_REMAINING_VALUE, remaining=format_hm(snap.remaining)),
            inline=True,
        )

        current = (
            say(TEXT.PROGRESS_CURRENT_VALUE, current_milestone=snap.current.hours)
            if snap.current
            else TEXT.PROGRESS_CURRENT_NONE
        )
        embed.add_field(name=TEXT.PROGRESS_CURRENT_FIELD, value=current, inline=True)

        if snap.upcoming and snap.time_to_next is not None:
            running = snap.status is EventStatus.RUNNING and snap.start_ts
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
            embed.add_field(
                name=TEXT.PROGRESS_COMPLETED_FIELD, value=TEXT.PROGRESS_COMPLETED_TEXT, inline=False
            )
        elif snap.status is EventStatus.CANCELLED:
            embed.add_field(
                name=TEXT.PROGRESS_CANCELLED_FIELD,
                value=snap.end_reason or TEXT.PROGRESS_CANCELLED_DEFAULT,
                inline=False,
            )
        if snap.grace_open:
            embed.colour = discord.Colour(color("GRACE"))
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
                TEXT.PROGRESS_FOOTER_LIVE
                if snap.status is EventStatus.RUNNING
                else TEXT.PROGRESS_FOOTER_FINAL
            )
        )
        return embed

    def render_progress(self, snap: Snapshot) -> str:
        return embed_to_text(self.build_progress(snap))

    async def update_progress(self, snap: Snapshot, *, force: bool = False) -> None:
        """Edit the single live progress message (creating it once if needed)."""
        embed = self.build_progress(snap)
        payload = embed_to_text(embed)
        if not force and payload == self._last_progress_payload:
            return  # nothing changed -> don't waste an API call

        msg = await self._get_progress_message()
        if msg is not None:
            try:
                await msg.edit(embed=embed, content=None, allowed_mentions=discord.AllowedMentions.none())
                self._last_progress_payload = payload
                return
            except discord.NotFound:
                log.warning("Progress message vanished — recreating it")
                self._progress_message = None
                self.engine.set_progress_message(None, None)
            except discord.Forbidden:
                log.error("Missing permission to edit the progress message")
                return
            except discord.HTTPException as exc:
                log.warning("Progress edit failed (will retry): %s", exc)
                return

        chan = await self.channel()
        if chan is None:
            return
        try:
            new_msg = await chan.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.error("Could not create the progress message: %s", exc)
            return
        self._progress_message = new_msg
        self._last_progress_payload = payload
        self.engine.set_progress_message(new_msg.channel.id, new_msg.id)
        try:
            await new_msg.pin(reason="Welcome to Hell live progress")
        except discord.HTTPException:
            pass  # pinning is a nicety, never a requirement

    async def _get_progress_message(self) -> Optional[discord.Message]:
        if self._progress_message is not None:
            return self._progress_message
        state = self.engine.state
        if not state.progress_message_id or not state.progress_channel_id:
            return None
        chan = self.bot.get_channel(state.progress_channel_id)
        if chan is None:
            try:
                chan = await self.bot.fetch_channel(state.progress_channel_id)
            except discord.HTTPException:
                return None
        try:
            self._progress_message = await chan.fetch_message(state.progress_message_id)  # type: ignore[union-attr]
        except discord.NotFound:
            self.engine.set_progress_message(None, None)
            return None
        except discord.HTTPException:
            return None
        return self._progress_message

    def forget_progress_message(self) -> None:
        self._progress_message = None
        self._last_progress_payload = None

    # ---------------------------------------------------------- event start

    def build_start(
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
            color=color("RUNNING"),
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
                    reward=self.config.reward_text(m.hours, m.reward),
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
        return embed

    def render_start(self, snap: Snapshot, host_mention: str, participants: Sequence[ParticipantRef]) -> str:
        return embed_to_text(self.build_start(snap, host_mention, participants))

    async def announce_start(
        self, snap: Snapshot, host: discord.abc.User, participants: Sequence[ParticipantRef]
    ) -> None:
        await self.send(
            [self.build_start(snap, host.mention, participants)],
            content="@everyone",
            mention_everyone=True,
        )

    # ---------------------------------------------------------- grace period

    def build_grace_warning(self, event: GraceStarted) -> discord.Embed:
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
            color=color("GRACE"),
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
        return embed

    def render_grace_warning(self, event: GraceStarted) -> str:
        return embed_to_text(self.build_grace_warning(event))

    async def announce_grace_warning(self, event: GraceStarted) -> None:
        # allowed_mentions is None-by-default in `send`: nobody is pinged here.
        await self.send([self.build_grace_warning(event)], mention_everyone=False)

    def build_grace_recovered(self, event: GraceRecovered) -> discord.Embed:
        fields = dict(
            empty_for=f"{event.empty_for:.0f}",
            participant_count=len(event.participants),
            vc=f"<#{self.config.voice_channel_id}>",
        )
        embed = discord.Embed(
            title=say(TEXT.GRACE_RECOVERED_TITLE, **fields),
            description=say(TEXT.GRACE_RECOVERED_DESCRIPTION, **fields),
            color=color("RUNNING"),
        )
        add_chunked_field(
            embed,
            say(TEXT.GRACE_RECOVERED_FIELD, **fields),
            format_members(event.participants, empty=TEXT.GRACE_RECOVERED_NOBODY),
        )
        embed.set_footer(text=say(TEXT.GRACE_RECOVERED_FOOTER, **fields))
        return embed

    def render_grace_recovered(self, event: GraceRecovered) -> str:
        return embed_to_text(self.build_grace_recovered(event))

    async def announce_grace_recovered(self, event: GraceRecovered) -> None:
        await self.send([self.build_grace_recovered(event)], mention_everyone=False)

    # ------------------------------------------------------------ milestones

    def build_milestone(self, event: MilestoneReached) -> discord.Embed:
        m = event.milestone
        fields = dict(
            hours=m.hours,
            remaining_hours=160 - m.hours,
            reward=self.config.reward_text(m.hours, m.reward),
            member_count=len(event.members),
            reached_at=discord_ts(event.reached_ts, "F"),
            reached_relative=discord_ts(event.reached_ts, "R"),
        )
        description = m.blurb
        if m.flavour:
            description = f"{m.blurb}\n\n*{m.flavour}*"
        embed = discord.Embed(
            title=m.title,
            description=description.strip(),
            color=color("COMPLETED") if m.hours == 160 else color("MILESTONE"),
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
        if event.late:
            embed.add_field(
                name=say(TEXT.MILESTONE_LATE_FIELD, **fields),
                value=say(TEXT.MILESTONE_LATE_TEXT, **fields),
                inline=False,
            )
        footer = TEXT.MILESTONE_FOOTER_FINAL if m.hours == 160 else TEXT.MILESTONE_FOOTER
        embed.set_footer(text=say(footer, **fields))
        return embed

    def render_milestone(self, event: MilestoneReached) -> str:
        return "@everyone\n" + embed_to_text(self.build_milestone(event))

    async def announce_milestone(self, event: MilestoneReached) -> None:
        sent = await self.send(
            [self.build_milestone(event)], content="@everyone", mention_everyone=True
        )
        if sent is not None:
            # Only flip the flag once the message really landed, so a failed
            # send is retried on the next startup instead of being lost.
            self.engine.mark_announced(event.milestone.hours)

    async def announce_pending(self, records: Iterable[MilestoneRecord]) -> None:
        """Re-send milestone messages claimed before a crash but never posted."""
        for rec in records:
            try:
                milestone = get_milestone(rec.hours)
            except KeyError:  # pragma: no cover - defensive
                continue
            await self.announce_milestone(
                MilestoneReached(
                    milestone=milestone,
                    reached_ts=rec.reached_ts,
                    members=list(rec.members),
                    late=True,
                )
            )

    # --------------------------------------------------------------- endings

    def build_failure(self, event: EventFailed) -> list[discord.Embed]:
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
            color=color("FAILED"),
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
        embed.set_footer(text=say(TEXT.FAILURE_FOOTER, **fields))
        return [embed] + self.build_leaderboard_embeds(
            event.leaderboard, title=TEXT.FAILURE_LEADERBOARD_TITLE, color=color("FAILED")
        )

    def render_failure(self, event: EventFailed) -> str:
        return embeds_to_text(self.build_failure(event))

    async def announce_failure(self, event: EventFailed) -> None:
        await self.send(self.build_failure(event), content="@everyone", mention_everyone=True)

    def build_cancelled(self, event: EventCancelled) -> list[discord.Embed]:
        fields = dict(
            who=f"<@{event.by_user_id}>" if event.by_user_id else "a host",
            elapsed=format_hms(event.elapsed),
            cancelled_at=discord_ts(event.cancelled_ts, "F"),
        )
        embed = discord.Embed(
            title=say(TEXT.CANCELLED_TITLE, **fields),
            description=say(TEXT.CANCELLED_DESCRIPTION, **fields),
            color=color("CANCELLED"),
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
        return [embed] + self.build_leaderboard_embeds(
            event.leaderboard, title=TEXT.CANCELLED_LEADERBOARD_TITLE, color=color("CANCELLED")
        )

    def render_cancelled(self, event: EventCancelled) -> str:
        return embeds_to_text(self.build_cancelled(event))

    async def announce_cancelled(self, event: EventCancelled) -> None:
        await self.send(self.build_cancelled(event))

    def build_completion(self, event: EventCompleted) -> list[discord.Embed]:
        board = event.leaderboard
        podium = top_n(board, 3)
        fields = dict(
            vc=f"<#{self.config.voice_channel_id}>",
            completed_at=discord_ts(event.completed_ts, "F"),
            final_reward=self.config.reward_text(160, get_milestone(160).reward),
            all_rewards=", ".join(
                self.config.reward_text(m.hours, m.short_reward or m.reward) for m in MILESTONES
            ),
            bonus_role=self.config.role_mention(
                self.config.cool_people_role_id, TEXT.TOP3_BONUS_ROLE
            ),
        )
        embed = discord.Embed(
            title=say(TEXT.COMPLETION_TITLE, **fields),
            description=say(TEXT.COMPLETION_DESCRIPTION, **fields),
            color=color("COMPLETED"),
        )
        embed.add_field(
            name=say(TEXT.COMPLETION_REWARD_FIELD, **fields),
            value=say(TEXT.COMPLETION_REWARD_TEXT, **fields),
            inline=False,
        )
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
        embed.set_footer(text=say(TEXT.COMPLETION_FOOTER, **fields))
        return [embed] + self.build_leaderboard_embeds(
            board, title=TEXT.COMPLETION_LEADERBOARD_TITLE, color=color("COMPLETED")
        )

    def render_completion(self, event: EventCompleted) -> str:
        return embeds_to_text(self.build_completion(event))

    async def announce_completion(self, event: EventCompleted) -> None:
        await self.send(self.build_completion(event), content="@everyone", mention_everyone=True)

    # ------------------------------------------------------------ views

    def build_status(self, snap: Snapshot, *, alive_line: Optional[str] = None) -> discord.Embed:
        embed = self.build_progress(snap)
        if alive_line:
            embed.add_field(name="🚨 Alive checks", value=alive_line, inline=False)
        records = self.engine.milestone_records()
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

    def render_status(self, snap: Snapshot, *, extra: Sequence[str] = ()) -> str:
        text = embed_to_text(self.build_status(snap))
        return "\n".join([text, *extra]) if extra else text

    def build_leaderboard_embeds(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        title: Optional[str] = None,
        color_value: Optional[int] = None,
        limit: int = 50,
        **legacy,
    ) -> list[discord.Embed]:
        """Podium + the rest, split across as many embeds as needed."""
        title = title if title is not None else TEXT.LEADERBOARD_TITLE
        colour = color_value if color_value is not None else legacy.get("color", color("RUNNING"))

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
        head.set_footer(text=say(TEXT.LEADERBOARD_FOOTER, total=total))
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

    def render_leaderboard_message(self, entries: Sequence[LeaderboardEntry], frozen: bool) -> str:
        title = TEXT.LEADERBOARD_TITLE_FINAL if frozen else TEXT.LEADERBOARD_TITLE
        text = embeds_to_text(self.build_leaderboard_embeds(entries, title=title))
        if frozen:
            text += f"\n\n*{TEXT.LEADERBOARD_FROZEN_FOOTER}*"
        return text

    # `render_leaderboard` from the pure module stays available for tests/CLI.
    plain_leaderboard = staticmethod(render_leaderboard)
