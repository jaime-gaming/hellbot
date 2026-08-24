"""Discord implementation of :class:`hell.alivecheck.AliveCheckIO`.

Handles the parts an alive check needs from the API: posting the roll call
with real pings, disconnecting the silent ones, posting the summary, and
reading back replies that arrived while the bot was offline.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Optional

import discord

from .config import Config

log = logging.getLogger("hell.aliveio")

MAX_CONTENT = 1900
MAX_MENTIONS_PER_MESSAGE = 60  # keep messages readable and inside the limit


class DiscordAliveCheckIO:
    """Talks to Discord on behalf of the alive-check manager."""

    def __init__(self, bot: discord.Client, config: Config):
        self.bot = bot
        self.config = config

    # ------------------------------------------------------------- channels

    def channel_id(self) -> int:
        """Where roll calls are posted.

        Defaults to the voice channel's own text chat (the "VC chat"), which is
        where the people being checked are already looking; override it with
        `ALIVE_CHECK_CHANNEL_ID`.
        """
        return self.config.alive_check_channel_id or self.config.voice_channel_id

    async def _channel(self) -> Optional[discord.abc.Messageable]:
        cid = self.channel_id()
        chan = self.bot.get_channel(cid)
        if chan is None:
            try:
                chan = await self.bot.fetch_channel(cid)
            except discord.HTTPException as exc:
                log.error("Alive-check channel %s unreachable: %s", cid, exc)
                return None
        if not isinstance(chan, discord.abc.Messageable):
            log.error("Alive-check channel %s cannot receive messages", cid)
            return None
        return chan

    # ------------------------------------------------------------- sending

    async def send_check(self, text: str, user_ids: Sequence[int]) -> Optional[tuple[int, int]]:
        chan = await self._channel()
        if chan is None:
            return None

        allowed = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)

        groups = [
            list(user_ids)[i : i + MAX_MENTIONS_PER_MESSAGE]
            for i in range(0, max(1, len(user_ids)), MAX_MENTIONS_PER_MESSAGE)
        ] or [[]]

        first: Optional[discord.Message] = None
        try:
            for index, group in enumerate(groups):
                mentions = " ".join(f"<@{uid}>" for uid in group)
                content = f"{mentions}\n{text}" if index == 0 else mentions
                msg = await chan.send(content[:MAX_CONTENT], allowed_mentions=allowed)
                first = first or msg
        except discord.Forbidden:
            log.error(
                "Missing permission to post the alive check in channel %s "
                "(needs View Channel + Send Messages).",
                self.channel_id(),
            )
            return None
        except discord.HTTPException as exc:
            log.error("Could not post the alive check: %s", exc)
            return None
        if first is None:
            return None
        return first.channel.id, first.id

    async def send_result(self, text: str) -> None:
        chan = await self._channel()
        if chan is None:
            return
        try:
            await chan.send(
                text[:MAX_CONTENT],
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=False, replied_user=False
                ),
            )
        except discord.HTTPException as exc:
            log.warning("Could not post the alive-check result: %s", exc)

    # -------------------------------------------------------------- kicking

    async def kick(self, user_ids: Sequence[int], reason: str) -> list[int]:
        guild = self.bot.get_guild(self.config.guild_id)
        channel = self.bot.get_channel(self.config.voice_channel_id)
        if guild is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            log.error(
                "Alive check wanted to disconnect %d user(s) but the guild or VC is not "
                "visible — nobody was removed",
                len(user_ids),
            )
            return []

        in_vc = {m.id: m for m in channel.members}
        removed: list[int] = []
        for uid in user_ids:
            member = in_vc.get(uid)
            if member is None:  # already left on their own
                continue
            try:
                await member.move_to(None, reason=reason)
                removed.append(uid)
                log.info("Alive check: disconnected %s (%s)", member.display_name, uid)
            except discord.Forbidden:
                log.error("Missing 'Move Members' — could not disconnect %s", uid)
            except discord.HTTPException as exc:
                log.warning("Could not disconnect %s: %s", uid, exc)
        return removed

    # --------------------------------------------------------------- muting

    async def mute(self, user_id: int, duration_seconds: int, reason: str) -> bool:
        """Mute / timeout a user on the server for the specified duration."""
        import datetime

        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            log.error("Cannot mute %d: guild %s not found", user_id, self.config.guild_id)
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.HTTPException, discord.Forbidden):
                member = None
        if member is None:
            log.warning("Cannot mute %d: member not in guild", user_id)
            return False
        try:
            until = discord.utils.utcnow() + datetime.timedelta(seconds=duration_seconds)
            await member.timeout(until, reason=reason)
            log.info("Muted/timed out %s (%d) for %ds: %s", member.display_name, user_id, duration_seconds, reason)
            return True
        except discord.Forbidden:
            log.warning("Missing 'Moderate Members' permission — could not mute %s", user_id)
            return False
        except discord.HTTPException as exc:
            log.warning("Could not mute %s: %s", user_id, exc)
            return False

    # ------------------------------------------------------------- recovery

    async def replies_since(
        self, channel_id: int, message_id: int, user_ids: Sequence[int]
    ) -> set[int]:
        """Scan messages posted after the roll call (used after a restart)."""
        from .alivecheck import is_valid_reply

        chan = self.bot.get_channel(channel_id)
        if chan is None:
            try:
                chan = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return set()
        if not isinstance(chan, discord.abc.Messageable):
            return set()

        wanted = set(user_ids)
        found: set[int] = set()
        try:
            async for message in chan.history(limit=500, after=discord.Object(id=message_id)):
                if message.author.id in wanted and is_valid_reply(
                    message.content, strict=self.config.alive_check_strict
                ):
                    found.add(message.author.id)
        except discord.Forbidden:
            log.warning("Missing 'Read Message History' — cannot recover alive-check replies")
        except discord.HTTPException as exc:
            log.warning("Could not read alive-check replies: %s", exc)
        return found
