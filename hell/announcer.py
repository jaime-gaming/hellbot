"""Reward / announcement system — every message the bot posts.

Kept apart from the engine so message wording can change without touching any
state logic.  All Discord API failures are swallowed and logged: a failed
message must never take the event timer down with it.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import discord

from .config import Config
from .engine import EventCancelled, EventCompleted, EventFailed, HellEngine, MilestoneReached, Snapshot
from .leaderboard import format_entry, render_leaderboard, top_n
from .milestones import MILESTONES, TOP3_BONUS_ROLE, get_milestone
from .models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from .timeutil import discord_ts, format_hm, format_hms, progress_bar

log = logging.getLogger("hell.announcer")

MAX_LEN = 1900  # keep a safety margin below Discord's 2000 character limit

STATUS_EMOJI = {
    EventStatus.IDLE: "💤",
    EventStatus.RUNNING: "🔥",
    EventStatus.FAILED: "💀",
    EventStatus.COMPLETED: "🏆",
    EventStatus.CANCELLED: "🛑",
}


def chunk_lines(lines: Sequence[str], limit: int = MAX_LEN) -> list[str]:
    """Split a list of lines into messages that fit Discord's length limit."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        line_len = len(line) + 1
        if size + line_len > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [""]


def format_members(members: Sequence[ParticipantRef], *, empty: str = "_nobody — the VC was empty_") -> str:
    if not members:
        return empty
    return ", ".join(m.mention() for m in members)


class Announcer:
    """Owns the announcement channel and the single live progress message."""

    def __init__(self, bot: discord.Client, config: Config, engine: HellEngine):
        self.bot = bot
        self.config = config
        self.engine = engine
        self._progress_message: Optional[discord.Message] = None

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

    async def send(self, content: str, *, mention_everyone: bool = False) -> Optional[discord.Message]:
        chan = await self.channel()
        if chan is None:
            return None
        allowed = discord.AllowedMentions(
            everyone=mention_everyone, users=False, roles=False, replied_user=False
        )
        first: Optional[discord.Message] = None
        try:
            for part in chunk_lines(content.split("\n")):
                msg = await chan.send(part, allowed_mentions=allowed)
                first = first or msg
        except discord.HTTPException as exc:
            log.error("Failed to send announcement: %s", exc)
        return first

    # ------------------------------------------------------------- progress

    def render_progress(self, snap: Snapshot) -> str:
        bar = progress_bar(snap.fraction)
        emoji = STATUS_EMOJI.get(snap.status, "🔥")
        lines = [
            f"{emoji} **WELCOME TO HELL** — `{snap.status.value}`",
            f"`{bar}` **{format_hm(snap.elapsed)} / {format_hm(snap.total)}**",
            f"**{snap.fraction * 100:.1f}%** complete",
            f"👥 Currently in Hell: **{snap.participants}**",
        ]
        if snap.current:
            lines.append(f"✅ Current milestone: **{snap.current.hours}h** cleared")
        else:
            lines.append("✅ Current milestone: **none yet**")
        if snap.upcoming and snap.time_to_next is not None:
            lines.append(f"🔥 Next milestone: **{snap.upcoming.hours}h** (in {format_hm(snap.time_to_next)})")
        else:
            lines.append("🔥 Next milestone: **none — all milestones cleared**")
        lines.append(f"⏳ {format_hm(snap.remaining)} remaining of the 160h challenge")
        if snap.start_ts:
            lines.append(f"🕛 Started {discord_ts(snap.start_ts, 'f')} ({discord_ts(snap.start_ts, 'R')})")
        if snap.status is EventStatus.FAILED:
            lines.append(f"💀 **FAILED** — {snap.end_reason or 'the VC became empty.'}")
        elif snap.status is EventStatus.COMPLETED:
            lines.append("🏆 **COMPLETED** — 160 consecutive hours survived.")
        elif snap.status is EventStatus.CANCELLED:
            lines.append(f"🛑 **CANCELLED** — {snap.end_reason or 'stopped by a host.'}")
        if snap.unverified >= 60:
            lines.append(
                f"⚠️ {format_hm(snap.unverified)} of the run could not be observed (bot downtime); "
                "the timer kept running and nobody was credited for that window."
            )
        return "\n".join(lines)

    async def update_progress(self, snap: Snapshot) -> None:
        """Edit the single live progress message (creating it once if needed)."""
        content = self.render_progress(snap)
        msg = await self._get_progress_message()
        if msg is not None:
            try:
                await msg.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.NotFound:
                log.warning("Progress message vanished — recreating")
                self._progress_message = None
                self.engine.set_progress_message(None, None)
            except discord.HTTPException as exc:
                log.warning("Progress edit failed: %s", exc)
                return

        chan = await self.channel()
        if chan is None:
            return
        try:
            new_msg = await chan.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.error("Could not create progress message: %s", exc)
            return
        self._progress_message = new_msg
        self.engine.set_progress_message(new_msg.channel.id, new_msg.id)

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
        except discord.HTTPException:
            self.engine.set_progress_message(None, None)
            return None
        return self._progress_message

    def forget_progress_message(self) -> None:
        self._progress_message = None

    # ---------------------------------------------------------- event start

    def render_start(self, snap: Snapshot, host_mention: str, participants: Sequence[ParticipantRef]) -> str:
        start_ts = snap.start_ts or 0
        lines = [
            "@everyone",
            "🔥 **WELCOME TO HELL HAS STARTED** 🔥",
            "",
            f"The challenge began {discord_ts(start_ts, 'F')} ({discord_ts(start_ts, 'R')}), started by {host_mention}.",
            f"<#{self.config.voice_channel_id}> must keep **at least one real human inside, continuously, for 160 hours**.",
            "",
            "**Rules**",
            "• Bots never count. `@clanker` users are removed on sight and earn no time.",
            "• AFK still counts — you just have to *be there*.",
            "• The second the VC is empty of valid humans, the run is **FAILED** and the timer stops forever.",
            "",
            "**Milestones**",
        ]
        for m in MILESTONES:
            lines.append(f"• **{m.hours}h** — {self.config.reward_text(m.hours, m.reward)}")
        lines += [
            "",
            f"Currently in Hell: **{len(participants)}** — {format_members(participants, empty='_nobody yet_')}",
            f"Ends at {discord_ts(start_ts + snap.total, 'F')} if the VC never empties.",
        ]
        return "\n".join(lines)

    async def announce_start(
        self, snap: Snapshot, host: discord.abc.User, participants: Sequence[ParticipantRef]
    ) -> None:
        await self.send(self.render_start(snap, host.mention, participants), mention_everyone=True)

    # ------------------------------------------------------------ milestones

    def render_milestone(self, event: MilestoneReached) -> str:
        m = event.milestone
        reward = self.config.reward_text(m.hours, m.reward)
        header = [
            "@everyone",
            f"**{m.title}**",
            "",
            m.blurb,
            f"**Reward:** {reward}",
        ]
        flavour = {
            32: "The first gate of Hell is behind you. 128 hours to go.",
            64: "Two milestones down. The VC has not been empty for a single second.",
            96: "Three gates cleared. From here, quitting would be a tragedy.",
            128: "Four gates cleared. 32 hours from history.",
            160: "There is nothing left to survive. Hell has been conquered.",
        }.get(m.hours)
        if flavour:
            header.append(f"_{flavour}_")
        header += [
            "",
            "⚠️ **Only users who are in the VC at this exact moment can claim this reward.**",
            f"🕛 Reached at {discord_ts(event.reached_ts, 'F')}",
            f"👥 **Eligible ({len(event.members)}):** {format_members(event.members)}",
        ]
        if event.late:
            header.append(
                "_(the bot was offline at the exact milestone second; this list is the first "
                "verified snapshot afterwards)_"
            )
        return "\n".join(header)

    async def announce_milestone(self, event: MilestoneReached) -> None:
        sent = await self.send(self.render_milestone(event), mention_everyone=True)
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

    def render_failure(self, event: EventFailed) -> str:
        lines = [
            "@everyone",
            "💀 **WELCOME TO HELL — CHALLENGE FAILED**",
            "",
            f"<#{self.config.voice_channel_id}> became **completely empty of valid participants**, "
            "so the run is over. The timer has stopped permanently and cannot resume.",
            "",
            f"⏱️ Survived: **{format_hms(event.elapsed)}** of 160h",
            f"🕛 Failed at {discord_ts(event.failed_ts, 'F')}",
        ]
        reached = [m for m in MILESTONES if m.seconds <= event.elapsed]
        lines.append(
            "🏁 Milestones secured: " + (", ".join(f"**{m.hours}h**" for m in reached) if reached else "**none**")
        )
        lines += ["", "The final leaderboard has been frozen:", "", render_leaderboard(event.leaderboard)]
        return "\n".join(lines)

    async def announce_failure(self, event: EventFailed) -> None:
        await self.send(self.render_failure(event), mention_everyone=True)

    def render_cancelled(self, event: EventCancelled) -> str:
        who = f"<@{event.by_user_id}>" if event.by_user_id else "a host"
        lines = [
            "🛑 **WELCOME TO HELL — CANCELLED**",
            "",
            f"The event was manually stopped by {who}. This is a **cancellation, not a failure** — "
            "the VC never emptied.",
            f"⏱️ Time on the clock: **{format_hms(event.elapsed)}** of 160h",
            "",
            render_leaderboard(event.leaderboard),
        ]
        return "\n".join(lines)

    async def announce_cancelled(self, event: EventCancelled) -> None:
        await self.send(self.render_cancelled(event))

    def render_completion(self, event: EventCompleted) -> str:
        board = event.leaderboard
        podium = top_n(board, 3)
        lines = [
            "@everyone",
            "🏆🔥 **WELCOME TO HELL HAS BEEN COMPLETED** 🔥🏆",
            "",
            "**160 consecutive hours.** The voice channel never emptied, not for one second.",
            f"🕛 Completed at {discord_ts(event.completed_ts, 'F')}",
            "",
            f"**Final milestone reward:** {self.config.reward_text(160, get_milestone(160).reward)} "
            "— for everyone in the VC at the 160h mark.",
            "",
            "**🥇 SPECIAL TOP 3 REWARD**",
            "The final Top 3 receive **every milestone reward** "
            f"({', '.join(self.config.reward_text(m.hours, m.short_reward or m.reward) for m in MILESTONES)}) "
            f"**plus {self.config.role_mention(self.config.cool_people_role_id, TOP3_BONUS_ROLE)}**.",
            "",
        ]
        if podium:
            lines.extend(format_entry(e) for e in podium)
        else:
            lines.append("_No ranked participants._")
        lines += ["", "**FINAL LEADERBOARD (frozen)**", "", render_leaderboard(board, title="🏆 FINAL RANKINGS")]
        return "\n".join(lines)

    async def announce_completion(self, event: EventCompleted) -> None:
        await self.send(self.render_completion(event), mention_everyone=True)

    # ---------------------------------------------------------------- views

    def render_status(self, snap: Snapshot, *, extra: Sequence[str] = ()) -> str:
        lines = [self.render_progress(snap)]
        records = self.engine.milestone_records()
        if records:
            lines.append("")
            lines.append("**Milestones reached**")
            for rec in records:
                lines.append(
                    f"• **{rec.hours}h** at {discord_ts(rec.reached_ts, 'f')} — "
                    f"{len(rec.members)} eligible user(s)"
                )
        lines.extend(extra)
        return "\n".join(lines)

    def render_leaderboard_message(self, entries: Sequence[LeaderboardEntry], frozen: bool) -> str:
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LEADERBOARD"
        body = render_leaderboard(entries, title=title)
        if frozen:
            body += "\n\n_These rankings are frozen; the event is over._"
        return body
