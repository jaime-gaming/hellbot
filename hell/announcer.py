"""Message delivery — channel resolution, sending, and the live progress message.

Layout lives in :mod:`hell.embeds`; wording lives in `Announcements.py`.  This
module is only concerned with *getting messages to Discord safely*:

* mentions are opt-in per message — only the start, milestone, failure and
  completion posts ping `@everyone`; the empty-VC warning deliberately pings
  nobody;
* payloads are split across messages when they would exceed Discord's limits;
* every API failure is logged and swallowed, because a message must never take
  the event timer down with it — milestones are flagged as announced only once
  Discord has actually accepted the post;
* the progress message is created once and then edited, and identical renders
  are skipped so the bot does not burn rate limit on unchanged content.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any, Optional

import discord

from . import assets
from .config import Config
from .difficulty import DifficultyInfo
from .embeds import (  # re-exported for callers and tests
    MAX_CONTENT,
    MAX_DESCRIPTION,
    MAX_EMBED_TOTAL,
    MAX_EMBEDS_PER_MESSAGE,
    MAX_FIELD,
    MAX_FIELDS,
    EmbedFactory,
    add_chunked_field,
    embed_to_text,
    embeds_to_text,
    format_members,
    split_text,
    status_color,
    status_emoji,
    theme_color,
)
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
from .finale import FinaleAnnouncement
from .hellevents import HellEventEnded, HellEventStarted
from .models import LeaderboardEntry, MilestoneRecord
from .texts import TEXT

log = logging.getLogger("hell.announcer")

# How many VC participants a Hell Event echo may ping (matches the roll calls).
MAX_VC_EVENT_PINGS = 60

__all__ = [
    "MAX_CONTENT",
    "MAX_DESCRIPTION",
    "MAX_EMBEDS_PER_MESSAGE",
    "MAX_EMBED_TOTAL",
    "MAX_FIELD",
    "MAX_FIELDS",
    "Announcer",
    "EmbedFactory",
    "add_chunked_field",
    "embed_to_text",
    "embeds_to_text",
    "format_members",
    "split_text",
    "status_color",
    "status_emoji",
    "theme_color",
]


class Announcer:
    """Owns the announcement channel and the single live progress message."""

    def __init__(self, bot: discord.Client, config: Config, engine: HellEngine):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.embeds = EmbedFactory(config)
        self._progress_message: Optional[discord.Message] = None
        self._last_progress_payload: Optional[str] = None

    # ------------------------------------------------------------- plumbing

    async def channel(self) -> Optional[discord.abc.Messageable]:
        """The configured announcement channel."""
        return await self.channel_for("announcements")

    async def channel_for(self, target: str = "announcements") -> Optional[discord.abc.Messageable]:
        """Resolve the announcement channel or the VC text chat."""
        if target == "vc":
            cid = self.config.alive_check_channel_id or self.config.voice_channel_id
            label = "VC text chat"
        else:
            cid = self.engine.state.announce_channel_id or self.config.announce_channel_id
            label = "announcement channel"
        chan = self.bot.get_channel(cid)
        if chan is None:
            try:
                chan = await self.bot.fetch_channel(cid)
            except discord.HTTPException as exc:
                log.error("%s %s unreachable: %s", label, cid, exc)
                return None
        if not isinstance(chan, discord.abc.Messageable):
            log.error("Configured %s %s is not a text channel", label, cid)
            return None
        return chan

    async def send(
        self,
        embeds: Sequence[discord.Embed],
        *,
        content: Optional[str] = None,
        mention_everyone: bool = False,
        target: str = "announcements",
        mention_users: bool = False,
    ) -> Optional[discord.Message]:
        """Post one or more embeds, chunked into as many messages as needed."""
        batch = list(embeds)
        if not batch and not content:
            return None  # Discord rejects a message with neither content nor embeds
        chan = await self.channel_for(target)
        if chan is None:
            return None
        allowed = discord.AllowedMentions(
            everyone=mention_everyone, users=mention_users, roles=False, replied_user=False
        )
        first: Optional[discord.Message] = None
        titles = ", ".join(e.title for e in batch if e.title) or "message"
        try:
            for index in range(0, max(1, len(batch)), MAX_EMBEDS_PER_MESSAGE):
                slice_ = batch[index : index + MAX_EMBEDS_PER_MESSAGE]
                files = assets.files_for(slice_)
                try:
                    msg = await chan.send(
                        content=(content if index == 0 else None),
                        embeds=slice_,
                        files=files,   # local artwork, if any
                        allowed_mentions=allowed,
                    )
                except discord.HTTPException:
                    assets.close_files(files)  # never leak artwork handles
                    raise
                first = first or msg
            log.info(
                "Announced: %s%s", titles, " (@everyone)" if mention_everyone else ""
            )
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

    # -------------------------------------------------------- embed builders
    # Thin delegates so callers (and tests) have one obvious entry point.

    def build_progress(self, snap: Snapshot) -> discord.Embed:
        return self.embeds.progress(snap)

    def build_start(self, snap: Snapshot, host_mention: str, participants) -> discord.Embed:
        return self.embeds.start(snap, host_mention, participants)

    def build_milestone(self, event: MilestoneReached) -> discord.Embed:
        return self.embeds.milestone(event, leaders=self.engine.leaderboard())

    def build_grace_warning(self, event: GraceStarted) -> discord.Embed:
        return self.embeds.grace_warning(event)

    def build_grace_recovered(self, event: GraceRecovered) -> discord.Embed:
        return self.embeds.grace_recovered(event)

    def build_failure(self, event: EventFailed) -> list[discord.Embed]:
        return self.embeds.failure(event)

    def build_cancelled(self, event: EventCancelled) -> list[discord.Embed]:
        return self.embeds.cancelled(event)

    def build_completion(self, event: EventCompleted) -> list[discord.Embed]:
        return self.embeds.completion(event)

    def build_leaderboard_embeds(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        title: Optional[str] = None,
        color: Optional[int] = None,
        limit: int = 50,
    ) -> list[discord.Embed]:
        return self.embeds.leaderboard(entries, title=title, color=color, limit=limit)

    def build_leaderboard_live_embeds(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        title: Optional[str] = None,
        color: Optional[int] = None,
        limit: int = 50,
        top_n: int = 5,
    ) -> list[discord.Embed]:
        return self.embeds.leaderboard_live(entries, title=title, color=color, limit=limit, top_n=top_n)

    def build_status(self, snap: Snapshot, *, alive_line: Optional[str] = None) -> discord.Embed:
        return self.embeds.status(
            snap, milestone_records=self.engine.milestone_records(), alive_line=alive_line
        )

    # ------------------------------------------------- plain-text renderings
    # Used by the offline simulator and the test-suite.

    def render_progress(self, snap: Snapshot) -> str:
        return embed_to_text(self.build_progress(snap))

    def render_start(self, snap: Snapshot, host_mention: str, participants) -> str:
        return embed_to_text(self.build_start(snap, host_mention, participants))

    def render_milestone(self, event: MilestoneReached) -> str:
        return "@everyone\n" + embed_to_text(self.build_milestone(event))

    def render_grace_warning(self, event: GraceStarted) -> str:
        return embed_to_text(self.build_grace_warning(event))

    def render_grace_recovered(self, event: GraceRecovered) -> str:
        return embed_to_text(self.build_grace_recovered(event))

    def render_failure(self, event: EventFailed) -> str:
        return embeds_to_text(self.build_failure(event))

    def render_cancelled(self, event: EventCancelled) -> str:
        return embeds_to_text(self.build_cancelled(event))

    def render_completion(self, event: EventCompleted) -> str:
        return embeds_to_text(self.build_completion(event))

    def render_leaderboard_message(self, entries: Sequence[LeaderboardEntry], frozen: bool) -> str:
        title = TEXT.LEADERBOARD_TITLE_FINAL if frozen else TEXT.LEADERBOARD_TITLE
        text = embeds_to_text(self.build_leaderboard_embeds(entries, title=title))
        if frozen:
            text += f"\n\n*{TEXT.LEADERBOARD_FROZEN_FOOTER}*"
        return text

    # -------------------------------------------------------- announcements

    async def announce_start(self, snap: Snapshot, host: discord.abc.User, participants) -> None:
        await self.send(
            [self.build_start(snap, host.mention, participants)],
            content="@everyone",
            mention_everyone=True,
        )

    async def announce_difficulty(self, diff: DifficultyInfo) -> Optional[discord.Message]:
        """Broadcast a difficulty update/escalation to the announcement channel."""
        return await self.send([self.embeds.difficulty_announcement(diff)])

    async def announce_difficulty_overview(self) -> Optional[discord.Message]:
        """Broadcast the full difficulty system breakdown to the announcement channel."""
        return await self.send(
            [self.embeds.difficulty_info(self.engine.elapsed(), override=self.engine.difficulty_override)]
        )

    async def announce_hell_event_start(self, event: HellEventStarted) -> Optional[discord.Message]:
        """Announce a Hell Event start in the VC text chat ONLY.

        The people affected are sitting in the VC — the announcement (with a
        ping) goes where they are looking, and nowhere else.
        """
        embed = self.embeds.hell_event_start(event)
        log.info("Announcing Hell Event start: %s (VC only)", event.record.name)
        return await self._send_hell_event_to_vc([embed], event.eligible_participants)

    async def announce_hell_event_end(self, event: HellEventEnded) -> Optional[discord.Message]:
        """Announce a Hell Event end in the VC text chat ONLY (no ping)."""
        embed = self.embeds.hell_event_end(event)
        log.info("Announcing Hell Event end: %s (VC only)", event.record.name)
        return await self._send_hell_event_to_vc([embed])

    async def _send_hell_event_to_vc(
        self,
        embeds: Sequence[discord.Embed],
        participants: Sequence[Any] = (),
    ) -> Optional[discord.Message]:
        """Post a Hell Event announcement in the VC text chat.

        Pings the affected participants (capped like the roll calls) so the
        event cannot be missed.  If the VC chat is unreachable the message is
        simply not delivered (logged by :meth:`send`) — hell events never fall
        back to the announcement channel.
        """
        uids = [p.user_id for p in participants][:MAX_VC_EVENT_PINGS]
        content = " ".join(f"<@{uid}>" for uid in uids) if uids else None
        return await self.send(
            list(embeds), content=content, target="vc", mention_users=bool(uids)
        )

    async def announce_finale_stage(self, ann: FinaleAnnouncement) -> Optional[discord.Message]:
        """Broadcast a 160-Hour Finale milestone stage to the announcement channel."""
        embed = self.embeds.finale_stage(ann)
        content = "@everyone" if ann.ping_everyone else None
        log.info("Announcing Finale stage: %s", ann.stage.value)
        return await self.send([embed], content=content, mention_everyone=ann.ping_everyone)

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
        from .milestones import get_milestone

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

    async def announce_grace_warning(self, event: GraceStarted) -> None:
        # `mention_everyone=False` plus users/roles off: this one pings nobody.
        await self.send([self.build_grace_warning(event)], mention_everyone=False)

    async def announce_grace_recovered(self, event: GraceRecovered) -> None:
        await self.send([self.build_grace_recovered(event)], mention_everyone=False)

    async def announce_failure(self, event: EventFailed) -> None:
        await self.send(self.build_failure(event), content="@everyone", mention_everyone=True)

    async def announce_cancelled(self, event: EventCancelled) -> None:
        await self.send(self.build_cancelled(event))

    async def announce_completion(self, event: EventCompleted) -> None:
        await self.send(self.build_completion(event), content="@everyone", mention_everyone=True)

    async def announce_continuation_resume(self) -> Optional[discord.Message]:
        """Post the Hell 2 (320h continuation) start announcement."""
        embed = self.embeds.continuation_resume()
        log.info("Announcing Hell 2 continuation resume")
        return await self.send([embed])

    # ------------------------------------------------ live progress message

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
                log.debug("Progress message updated")
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
        files = assets.files_for([embed])
        try:
            # Artwork is uploaded once, with the message; later edits keep it.
            new_msg = await chan.send(
                embed=embed,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            assets.close_files(files)  # never leak artwork handles
            log.error("Could not create the progress message: %s", exc)
            return
        self._progress_message = new_msg
        self._last_progress_payload = payload
        self.engine.set_progress_message(new_msg.channel.id, new_msg.id)
        log.info("Live progress message created (%s) in #%s", new_msg.id, new_msg.channel.id)
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
