"""Voice channel monitoring.

Runs two loops:

* **1 second** — read the target VC, drop bots, kick `@clanker` users, feed a
  trusted :class:`~hell.engine.Observation` into the engine and dispatch any
  domain events (milestone / failure / completion) to the announcer.
* **10 seconds** — edit the live progress message.

An observation is only fed to the engine when it can be *trusted*: the gateway
is connected, the guild and the voice channel resolved, and the startup grace
window has elapsed.  Otherwise the bot would risk failing the event just
because its cache was cold after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

import discord
from discord.ext import tasks

from .announcer import Announcer
from .config import Config
from .engine import (
    EventCancelled,
    EventCompleted,
    EventFailed,
    HellEngine,
    MilestoneReached,
    Observation,
)
from .models import EventStatus, ParticipantRef
from .timeutil import format_hm, now_ts

log = logging.getLogger("hell.monitor")


def participant_ref(member: discord.Member) -> ParticipantRef:
    return ParticipantRef(user_id=member.id, display_name=member.display_name)


class VoiceMonitor:
    """Owns the polling loops and all Discord-side VC logic."""

    def __init__(self, bot: discord.Client, config: Config, engine: HellEngine, announcer: Announcer):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.announcer = announcer
        self._ready_at: Optional[float] = None
        self._kick_attempts: dict[int, float] = {}
        self._lock = asyncio.Lock()  # serialises ticks; no milestone can race
        self._monitor_loop.change_interval(seconds=max(0.25, config.monitor_interval))
        self._progress_loop.change_interval(seconds=max(1.0, config.progress_interval))

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._ready_at = now_ts()
        if not self._monitor_loop.is_running():
            self._monitor_loop.start()
        if not self._progress_loop.is_running():
            self._progress_loop.start()

    def stop(self) -> None:
        self._monitor_loop.cancel()
        self._progress_loop.cancel()

    @property
    def lock(self) -> asyncio.Lock:
        """Shared with the command layer so state changes never race a tick."""
        return self._lock

    @property
    def grace_active(self) -> bool:
        if self._ready_at is None:
            return True
        return (now_ts() - self._ready_at) < self.config.startup_grace

    # ----------------------------------------------------------- VC reading

    def voice_channel(self) -> Optional[discord.VoiceChannel]:
        cid = self.engine.state.voice_channel_id or self.config.voice_channel_id
        channel = self.bot.get_channel(cid)
        if channel is None:
            return None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            log.error("Channel %s is not a voice channel", cid)
            return None
        return channel  # type: ignore[return-value]

    def is_clanker(self, member: discord.Member) -> bool:
        return any(r.id == self.config.clanker_role_id for r in member.roles)

    async def collect(self) -> Optional[tuple[list[ParticipantRef], list[discord.Member]]]:
        """Return `(valid humans, clankers to kick)` or None if untrusted."""
        if not self.bot.is_ready() or self.bot.is_closed():
            return None
        channel = self.voice_channel()
        if channel is None:
            log.warning("Target voice channel %s not in cache yet", self.config.voice_channel_id)
            return None

        humans: list[ParticipantRef] = []
        clankers: list[discord.Member] = []
        for member in channel.members:
            if member.bot:  # bots never count, ever
                continue
            if self.is_clanker(member):
                clankers.append(member)
                continue
            humans.append(participant_ref(member))
        return humans, clankers

    async def kick_clankers(self, members: Sequence[discord.Member]) -> None:
        """Disconnect `@clanker` users from the VC immediately."""
        now = now_ts()
        for member in members:
            last = self._kick_attempts.get(member.id, 0.0)
            if now - last < 3.0:  # avoid hammering the API if a kick is failing
                continue
            self._kick_attempts[member.id] = now
            try:
                await member.move_to(None, reason="Welcome to Hell: @clanker is not allowed in the VC")
                log.info("Kicked @clanker %s (%s) from the VC", member.display_name, member.id)
            except discord.Forbidden:
                log.error("Missing permission to disconnect clanker %s", member.id)
            except discord.HTTPException as exc:
                log.warning("Failed to disconnect clanker %s: %s", member.id, exc)

    # ----------------------------------------------------------------- loops

    @tasks.loop(seconds=1.0)
    async def _monitor_loop(self) -> None:
        async with self._lock:
            await self._tick_once()

    @_monitor_loop.before_loop
    async def _before_monitor(self) -> None:
        await self.bot.wait_until_ready()

    @_monitor_loop.error
    async def _monitor_error(self, exc: BaseException) -> None:  # pragma: no cover - safety net
        log.exception("Monitor loop crashed, restarting it", exc_info=exc)
        await asyncio.sleep(1)
        self._monitor_loop.restart()

    async def _tick_once(self) -> None:
        if not self.engine.is_running:
            return
        collected = await self.collect()
        if collected is None:
            return
        humans, clankers = collected
        if clankers:
            await self.kick_clankers(clankers)
        if self.grace_active:
            # Cache may still be warming up: observe, but never fail the event.
            log.debug("Startup grace active — skipping engine tick (%d humans seen)", len(humans))
            return

        events = self.engine.tick(Observation(now=now_ts(), participants=tuple(humans)))
        for event in events:
            await self.dispatch(event)

    @tasks.loop(seconds=10.0)
    async def _progress_loop(self) -> None:
        state = self.engine.state
        if state.status is EventStatus.IDLE:
            return
        if state.status.is_terminal and state.progress_message_id is None:
            return
        count = len(self.engine.last_participants)
        await self.announcer.update_progress(self.engine.snapshot(participants=count))

    @_progress_loop.before_loop
    async def _before_progress(self) -> None:
        await self.bot.wait_until_ready()

    @_progress_loop.error
    async def _progress_error(self, exc: BaseException) -> None:  # pragma: no cover - safety net
        log.exception("Progress loop crashed, restarting it", exc_info=exc)
        await asyncio.sleep(2)
        self._progress_loop.restart()

    # ------------------------------------------------------------- dispatch

    async def dispatch(self, event: object) -> None:
        if isinstance(event, MilestoneReached):
            await self.announcer.announce_milestone(event)
        elif isinstance(event, EventFailed):
            await self.announcer.announce_failure(event)
            await self._final_progress()
        elif isinstance(event, EventCompleted):
            await self.announcer.announce_completion(event)
            await self._final_progress()
        elif isinstance(event, EventCancelled):
            await self.announcer.announce_cancelled(event)
            await self._final_progress()

    async def _final_progress(self) -> None:
        await self.announcer.update_progress(self.engine.snapshot(participants=len(self.engine.last_participants)))

    # -------------------------------------------------------------- recovery

    async def resume_after_restart(self) -> None:
        """Bring a RUNNING event back to life after the process restarted."""
        state = self.engine.state
        if state.status is not EventStatus.RUNNING:
            return
        gap = now_ts() - (state.last_tick_ts or state.start_ts or now_ts())
        log.info(
            "Resuming event %s — elapsed %s, unobserved gap %.0fs",
            state.event_uid,
            format_hm(self.engine.elapsed()),
            gap,
        )
        # Any milestone claimed but never announced (crash between the two).
        pending = self.engine.pending_announcements()
        if pending:
            log.warning("Re-announcing %d milestone(s) that were never posted", len(pending))
            await self.announcer.announce_pending(pending)
        self.announcer.forget_progress_message()
        await self.announcer.update_progress(self.engine.snapshot())
