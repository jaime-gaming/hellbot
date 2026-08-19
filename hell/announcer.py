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
from .milestones import MILESTONES, TOP3_BONUS_ROLE, get_milestone
from .models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from .timeutil import discord_ts, format_hm, format_hms, progress_bar

log = logging.getLogger("hell.announcer")

MAX_CONTENT = 2000
MAX_DESCRIPTION = 4000      # 4096 hard limit, margin kept
MAX_FIELD = 1000            # 1024 hard limit, margin kept
MAX_EMBED_TOTAL = 5500      # 6000 hard limit, margin kept
MAX_FIELDS = 20             # 25 hard limit, margin kept
MAX_EMBEDS_PER_MESSAGE = 10

COLOR_RUNNING = 0xE25822    # ember orange
COLOR_MILESTONE = 0xFF4500
COLOR_FAILED = 0x8B0000
COLOR_COMPLETED = 0xFFD700
COLOR_CANCELLED = 0x607D8B
COLOR_GRACE = 0xFFA500
COLOR_IDLE = 0x2F3136

STATUS_EMOJI = {
    EventStatus.IDLE: "💤",
    EventStatus.RUNNING: "🔥",
    EventStatus.FAILED: "💀",
    EventStatus.COMPLETED: "🏆",
    EventStatus.CANCELLED: "🛑",
}

STATUS_COLOR = {
    EventStatus.IDLE: COLOR_IDLE,
    EventStatus.RUNNING: COLOR_RUNNING,
    EventStatus.FAILED: COLOR_FAILED,
    EventStatus.COMPLETED: COLOR_COMPLETED,
    EventStatus.CANCELLED: COLOR_CANCELLED,
}


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


def format_members(members: Sequence[ParticipantRef], *, empty: str = "*nobody — the VC was empty*") -> str:
    if not members:
        return empty
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
        emoji = STATUS_EMOJI.get(snap.status, "🔥")
        bar = progress_bar(snap.fraction)
        embed = discord.Embed(
            title=f"{emoji} WELCOME TO HELL",
            description=(
                f"`{bar}`\n"
                f"**{format_hm(snap.elapsed)} / {format_hm(snap.total)}** — "
                f"**{snap.fraction * 100:.1f}%** complete"
            ),
            color=STATUS_COLOR.get(snap.status, COLOR_RUNNING),
        )
        status_value = f"`{snap.status.value}`"
        if snap.grace_open:
            status_value = f"`{snap.status.value}` ⚠️ **VC EMPTY**"
        embed.add_field(name="Status", value=status_value, inline=True)
        embed.add_field(name="👥 Currently in Hell", value=f"**{snap.participants}**", inline=True)
        embed.add_field(name="⏳ Time remaining", value=f"**{format_hm(snap.remaining)}**", inline=True)

        current = f"**{snap.current.hours}h** cleared" if snap.current else "*none yet*"
        embed.add_field(name="✅ Current milestone", value=current, inline=True)
        if snap.upcoming and snap.time_to_next is not None:
            nxt = f"**{snap.upcoming.hours}h**\nin {format_hm(snap.time_to_next)}"
            if snap.status is EventStatus.RUNNING and snap.start_ts:
                nxt += f"\n({discord_ts(snap.start_ts + snap.upcoming.seconds, 'R')})"
        else:
            nxt = "*all milestones cleared*"
        embed.add_field(name="🔥 Next milestone", value=nxt, inline=True)

        if snap.start_ts:
            when = f"{discord_ts(snap.start_ts, 'f')}\n{discord_ts(snap.start_ts, 'R')}"
            embed.add_field(name="🕛 Started", value=when, inline=True)

        if snap.status is EventStatus.FAILED:
            embed.add_field(
                name="💀 FAILED",
                value=snap.end_reason or "The VC became empty of valid participants.",
                inline=False,
            )
        elif snap.status is EventStatus.COMPLETED:
            embed.add_field(
                name="🏆 COMPLETED",
                value="160 consecutive hours survived. Welcome to Hell has been completed.",
                inline=False,
            )
        elif snap.status is EventStatus.CANCELLED:
            embed.add_field(
                name="🛑 CANCELLED",
                value=snap.end_reason or "Manually stopped by a host.",
                inline=False,
            )
        if snap.grace_open:
            embed.colour = discord.Colour(COLOR_GRACE)
            embed.add_field(
                name="⚠️ THE VC IS EMPTY",
                value=(
                    f"**{snap.grace_seconds_left:.0f}s** left to get somebody back in "
                    f"<#{self.config.voice_channel_id}> or the run is over."
                ),
                inline=False,
            )
        if snap.unverified >= 60:
            embed.add_field(
                name="⚠️ Unobserved window",
                value=(
                    f"{format_hm(snap.unverified)} of this run could not be watched (bot offline). "
                    "The timer kept running; nobody was credited for that window."
                ),
                inline=False,
            )
        if snap.status is EventStatus.RUNNING:
            embed.set_footer(text="Live • updates every 10 seconds • leave the VC empty and it all ends")
        else:
            embed.set_footer(text="Final state • this message is no longer updating")
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
        embed = discord.Embed(
            title="🔥 WELCOME TO HELL HAS STARTED 🔥",
            description=(
                f"The gates are open. Starting **now**, <#{self.config.voice_channel_id}> must keep "
                "**at least one real human inside, continuously, for 160 hours**.\n\n"
                f"Started by {host_mention} • {discord_ts(start_ts, 'F')} ({discord_ts(start_ts, 'R')})"
            ),
            color=COLOR_RUNNING,
        )
        embed.add_field(
            name="📜 The rules",
            value=(
                "• Bots never count.\n"
                f"• <@&{self.config.clanker_role_id}> users are removed on sight and earn no time.\n"
                "• AFK still counts — you just have to *be there*.\n"
                "• **Random alive checks**: every 1–6 hours everyone in the VC gets pinged and has "
                "5 minutes to reply `Yes`. Miss it and you are disconnected — your leaderboard time "
                "stays and you can rejoin instantly.\n"
                "• The second the VC empties of valid humans, the run is **FAILED**, forever."
            ),
            inline=False,
        )
        embed.add_field(
            name="🏁 Milestones",
            value="\n".join(
                f"**{m.hours}h** — {self.config.reward_text(m.hours, m.reward)}" for m in MILESTONES
            ),
            inline=False,
        )
        add_chunked_field(
            embed,
            f"👥 In Hell right now ({len(participants)})",
            format_members(participants, empty="*nobody yet*"),
        )
        embed.add_field(
            name="🕛 Finish line",
            value=f"{discord_ts(start_ts + snap.total, 'F')}\n({discord_ts(start_ts + snap.total, 'R')})",
            inline=False,
        )
        embed.set_footer(text="Good luck. You are going to need it. • /hell status • /hell leaderboard")
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
        embed = discord.Embed(
            title="⚠️ THE VC IS EMPTY — THE RUN IS ABOUT TO DIE",
            description=(
                f"<#{self.config.voice_channel_id}> has **no valid humans** in it.\n"
                f"Somebody has **{event.seconds:.0f} seconds** to join or "
                "**Welcome to Hell fails permanently**."
            ),
            color=COLOR_GRACE,
        )
        embed.add_field(
            name="⏳ Deadline",
            value=f"{discord_ts(event.deadline_ts, 'T')} ({discord_ts(event.deadline_ts, 'R')})",
            inline=True,
        )
        embed.add_field(name="⏱️ On the clock", value=f"**{format_hm(event.elapsed)}** / 160h", inline=True)
        embed.set_footer(text="No pings on purpose — if you are reading this, get in the VC.")
        return embed

    def render_grace_warning(self, event: GraceStarted) -> str:
        return embed_to_text(self.build_grace_warning(event))

    async def announce_grace_warning(self, event: GraceStarted) -> None:
        # allowed_mentions is None-by-default in `send`: nobody is pinged here.
        await self.send([self.build_grace_warning(event)], mention_everyone=False)

    def build_grace_recovered(self, event: GraceRecovered) -> discord.Embed:
        embed = discord.Embed(
            title="✅ SAVED — THE RUN CONTINUES",
            description=(
                f"The VC was empty for **{event.empty_for:.0f}s** and somebody made it back "
                "in time. The 160h clock never stopped."
            ),
            color=COLOR_RUNNING,
        )
        embed.add_field(
            name=f"👥 Back in Hell ({len(event.participants)})",
            value=format_members(event.participants, empty="*nobody*"),
            inline=False,
        )
        embed.set_footer(text="That was close.")
        return embed

    def render_grace_recovered(self, event: GraceRecovered) -> str:
        return embed_to_text(self.build_grace_recovered(event))

    async def announce_grace_recovered(self, event: GraceRecovered) -> None:
        await self.send([self.build_grace_recovered(event)], mention_everyone=False)

    # ------------------------------------------------------------ milestones

    FLAVOUR = {
        32: "The first gate is behind you. 128 hours to go — the easy part is over.",
        64: "Two gates down. This VC has not been silent for a single second.",
        96: "Three gates cleared. Quitting now would be a tragedy for everyone involved.",
        128: "Four gates cleared. Thirty-two hours from history.",
        160: "There is nothing left to survive. Hell has been conquered.",
    }

    def build_milestone(self, event: MilestoneReached) -> discord.Embed:
        m = event.milestone
        embed = discord.Embed(
            title=m.title,
            description=f"{m.blurb}\n\n*{self.FLAVOUR.get(m.hours, '')}*".strip(),
            color=COLOR_COMPLETED if m.hours == 160 else COLOR_MILESTONE,
        )
        embed.add_field(name="🎁 Reward", value=self.config.reward_text(m.hours, m.reward), inline=False)
        embed.add_field(
            name="⚠️ How to claim",
            value=(
                "**Only the users listed below — the ones in the VC at this exact moment — "
                "can claim this reward.** The list is recorded and timestamped; joining afterwards "
                "does not count."
            ),
            inline=False,
        )
        add_chunked_field(embed, f"👥 Eligible ({len(event.members)})", format_members(event.members))
        embed.add_field(
            name="🕛 Reached at",
            value=f"{discord_ts(event.reached_ts, 'F')} ({discord_ts(event.reached_ts, 'R')})",
            inline=False,
        )
        if event.late:
            embed.add_field(
                name="ℹ️ Note",
                value=(
                    "The bot was offline at the exact milestone second; this list is the first "
                    "verified snapshot taken afterwards."
                ),
                inline=False,
            )
        remaining = 160 - m.hours
        embed.set_footer(
            text=(
                f"Milestone {m.hours}h of 160h"
                + (f" • {remaining}h left" if remaining else " • FINAL MILESTONE")
            )
        )
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
        embed = discord.Embed(
            title="💀 WELCOME TO HELL — CHALLENGE FAILED",
            description=(
                f"<#{self.config.voice_channel_id}> was **completely empty of valid participants** "
                "for the entire grace period, so nobody came back in time.\n"
                "The timer has stopped **permanently** and the run cannot resume."
            ),
            color=COLOR_FAILED,
        )
        embed.add_field(name="⏱️ Survived", value=f"**{format_hms(event.elapsed)}** of 160h", inline=True)
        embed.add_field(
            name="📉 Progress",
            value=f"**{event.elapsed / (160 * 3600) * 100:.1f}%**",
            inline=True,
        )
        embed.add_field(name="🕛 Failed at", value=discord_ts(event.failed_ts, "F"), inline=True)
        embed.add_field(
            name="🏁 Milestones secured",
            value=", ".join(f"**{m.hours}h**" for m in reached) if reached else "**none**",
            inline=False,
        )
        embed.set_footer(text="Rewards already earned at reached milestones still stand. Reset with /hell reset.")
        return [embed] + self.build_leaderboard_embeds(
            event.leaderboard, title="🏆 FINAL LEADERBOARD (frozen)", color=COLOR_FAILED
        )

    def render_failure(self, event: EventFailed) -> str:
        return embeds_to_text(self.build_failure(event))

    async def announce_failure(self, event: EventFailed) -> None:
        await self.send(self.build_failure(event), content="@everyone", mention_everyone=True)

    def build_cancelled(self, event: EventCancelled) -> list[discord.Embed]:
        who = f"<@{event.by_user_id}>" if event.by_user_id else "a host"
        embed = discord.Embed(
            title="🛑 WELCOME TO HELL — CANCELLED",
            description=(
                f"The event was manually stopped by {who}.\n"
                "This is a **cancellation, not a failure** — the VC never emptied."
            ),
            color=COLOR_CANCELLED,
        )
        embed.add_field(name="⏱️ Time on the clock", value=f"**{format_hms(event.elapsed)}** of 160h", inline=True)
        embed.add_field(name="🕛 Stopped at", value=discord_ts(event.cancelled_ts, "F"), inline=True)
        embed.set_footer(text="A host can begin a fresh run with /hell reset followed by /hell start.")
        return [embed] + self.build_leaderboard_embeds(
            event.leaderboard, title="🏆 LEADERBOARD (frozen)", color=COLOR_CANCELLED
        )

    def render_cancelled(self, event: EventCancelled) -> str:
        return embeds_to_text(self.build_cancelled(event))

    async def announce_cancelled(self, event: EventCancelled) -> None:
        await self.send(self.build_cancelled(event))

    def build_completion(self, event: EventCompleted) -> list[discord.Embed]:
        board = event.leaderboard
        podium = top_n(board, 3)
        embed = discord.Embed(
            title="🏆🔥 WELCOME TO HELL HAS BEEN COMPLETED 🔥🏆",
            description=(
                "**160 consecutive hours.**\n"
                f"<#{self.config.voice_channel_id}> never emptied — not for one single second.\n\n"
                f"Completed {discord_ts(event.completed_ts, 'F')}."
            ),
            color=COLOR_COMPLETED,
        )
        embed.add_field(
            name="🎁 160h reward",
            value=(
                f"{self.config.reward_text(160, get_milestone(160).reward)}\n"
                "*for everyone who was in the VC at the 160h mark*"
            ),
            inline=False,
        )
        add_chunked_field(
            embed,
            "🥇 Special Top 3 reward",
            (
                "The final Top 3 receive **every milestone reward** — "
                + ", ".join(self.config.reward_text(m.hours, m.short_reward or m.reward) for m in MILESTONES)
                + " — **plus "
                + self.config.role_mention(self.config.cool_people_role_id, TOP3_BONUS_ROLE)
                + "**."
            ),
        )
        if podium:
            add_chunked_field(embed, "🏅 The Top 3", "\n".join(format_entry(e) for e in podium))
        embed.set_footer(text="The leaderboard below is final and frozen. Well done, all of you.")
        return [embed] + self.build_leaderboard_embeds(
            board, title="🏆 FINAL RANKINGS (frozen)", color=COLOR_COMPLETED
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
        title: str = "🏆 WELCOME TO HELL — LEADERBOARD",
        color: int = COLOR_RUNNING,
        limit: int = 50,
    ) -> list[discord.Embed]:
        """Podium + the rest, split across as many embeds as needed."""
        if not entries:
            return [
                discord.Embed(
                    title=title,
                    description="*Nobody has spent time in Hell yet.*",
                    color=color,
                )
            ]

        shown = list(entries[:limit])
        podium = [e for e in shown if e.rank <= 3]
        rest = [e for e in shown if e.rank > 3]

        head = discord.Embed(
            title=title,
            description="\n".join(format_entry(e) for e in podium) or "*No podium yet.*",
            color=color,
        )
        total = len(entries)
        head.set_footer(
            text=f"{total} participant(s) • times count only while the event is running"
        )
        embeds = [head]
        if rest:
            for index, chunk in enumerate(split_text("\n".join(format_entry(e) for e in rest), MAX_DESCRIPTION)):
                embeds.append(
                    discord.Embed(
                        title="Everyone else" if index == 0 else "Everyone else (cont.)",
                        description=chunk,
                        color=color,
                    )
                )
        hidden = total - len(shown)
        if hidden > 0:
            embeds[-1].add_field(name="…", value=f"and {hidden} more participant(s)", inline=False)
        return embeds[:MAX_EMBEDS_PER_MESSAGE]

    def render_leaderboard_message(self, entries: Sequence[LeaderboardEntry], frozen: bool) -> str:
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LEADERBOARD"
        text = embeds_to_text(self.build_leaderboard_embeds(entries, title=title))
        if frozen:
            text += "\n\n*These rankings are frozen; the event is over.*"
        return text

    # `render_leaderboard` from the pure module stays available for tests/CLI.
    plain_leaderboard = staticmethod(render_leaderboard)
