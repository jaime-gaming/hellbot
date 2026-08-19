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

from .alivecheck import AliveCheckManager
from .aliveio import DiscordAliveCheckIO
from .announcer import Announcer
from .config import Config
from .dm import FinalReportDM
from .engine import (
    EventCancelled,
    EventCompleted,
    EventFailed,
    GraceRecovered,
    GraceStarted,
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
        self.alive_io = DiscordAliveCheckIO(bot, config)
        self.alive_checks = AliveCheckManager(config, engine.store, self.alive_io)
        self.alive_checks.bind(engine.event_uid)
        self.reports = FinalReportDM(bot, config, engine, announcer)
        self._ready_at: Optional[float] = None
        self._kick_attempts: dict[int, float] = {}
        self._lock = asyncio.Lock()  # serialises ticks; no milestone can race
        self._blind_since: Optional[float] = None
        self._blind_logged = False
        self._last_heartbeat = 0.0
        self._terminal_rendered = False
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
            self._note_blind("gateway not ready")
            return None
        channel = self.voice_channel()
        if channel is None:
            self._note_blind(f"voice channel {self.config.voice_channel_id} not visible")
            return None
        self._note_sighted()

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

    def _note_blind(self, reason: str) -> None:
        """Remember that the bot currently cannot observe the VC."""
        now = now_ts()
        if self._blind_since is None:
            self._blind_since = now
        elif not self._blind_logged and (now - self._blind_since) > 30:
            self._blind_logged = True
            log.error(
                "Cannot observe the target VC for %.0fs (%s). The event timer keeps running, "
                "but presence is not being verified.",
                now - self._blind_since,
                reason,
            )

    def _note_sighted(self) -> None:
        if self._blind_since is not None:
            blind_for = now_ts() - self._blind_since
            if blind_for > 5:
                log.info("Voice channel visible again after %.0fs", blind_for)
            self._blind_since = None
            self._blind_logged = False

    @property
    def blind_seconds(self) -> float:
        return 0.0 if self._blind_since is None else now_ts() - self._blind_since

    async def kick_clankers(self, members: Sequence[discord.Member]) -> None:
        """Disconnect `@clanker` users from the VC immediately."""
        now = now_ts()
        if len(self._kick_attempts) > 256:  # keep the cooldown map from growing forever
            self._kick_attempts = {k: v for k, v in self._kick_attempts.items() if now - v < 60}
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

        now = now_ts()
        events = self.engine.tick(Observation(now=now, participants=tuple(humans)))
        for event in events:
            await self.dispatch(event)

        if self.engine.is_running:
            # Roll call: random every 1-6h, resolved 5 minutes later. Kicked
            # users keep their leaderboard time and may rejoin immediately.
            try:
                await self.alive_checks.tick(now, humans)
            except Exception:  # pragma: no cover - never break the event loop
                log.exception("Alive check tick failed")
        self._heartbeat(len(humans))

    def _heartbeat(self, participants: int) -> None:
        """Periodic proof-of-life in the log file, useful when running headless."""
        interval = max(60.0, self.config.heartbeat_minutes * 60.0)
        now = now_ts()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        snap = self.engine.snapshot(now=now, participants=participants)
        log.info(
            "Heartbeat: %s | %s / %s (%.1f%%) | %d in VC | next milestone: %s",
            snap.status.value,
            format_hm(snap.elapsed),
            format_hm(snap.total),
            snap.fraction * 100,
            participants,
            f"{snap.upcoming.hours}h" if snap.upcoming else "none",
        )

    @tasks.loop(seconds=10.0)
    async def _progress_loop(self) -> None:
        state = self.engine.state
        if state.status is EventStatus.IDLE:
            return
        if state.status.is_terminal:
            # Render the final state exactly once, then stop burning API calls.
            if self._terminal_rendered or state.progress_message_id is None:
                return
            self._terminal_rendered = True
        else:
            self._terminal_rendered = False
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
        if isinstance(event, (EventFailed, EventCompleted, EventCancelled)):
            # A roll call in flight when the run ends is dropped, not enforced.
            if self.alive_checks.pending is not None:
                await self.alive_checks.cancel(now_ts(), "the event ended")
        if isinstance(event, GraceStarted):
            # Warning only, and deliberately without pinging anybody.
            await self.announcer.announce_grace_warning(event)
            await self._final_progress(terminal=False)
        elif isinstance(event, GraceRecovered):
            await self.announcer.announce_grace_recovered(event)
            await self._final_progress(terminal=False)
        elif isinstance(event, MilestoneReached):
            await self.announcer.announce_milestone(event)
        elif isinstance(event, EventFailed):
            await self.announcer.announce_failure(event)
            await self._final_progress()
            self.reports.schedule()          # DM every contestant their stats
        elif isinstance(event, EventCompleted):
            await self.announcer.announce_completion(event)
            await self._final_progress()
            self.reports.schedule()
        elif isinstance(event, EventCancelled):
            await self.announcer.announce_cancelled(event)
            await self._final_progress()
            self.reports.schedule()

    async def _final_progress(self, *, terminal: bool = True) -> None:
        self._terminal_rendered = terminal
        await self.announcer.update_progress(
            self.engine.snapshot(participants=len(self.engine.last_participants)), force=True
        )

    # ---------------------------------------------------------- alive checks

    async def handle_message(self, message: discord.Message) -> None:
        """Route a chat message to the pending alive check, if any."""
        if message.author.bot or not self.engine.is_running:
            return
        pending = self.alive_checks.pending
        if pending is None:
            return
        if self.alive_checks.register_reply(message.author.id, message.content, message.channel.id):
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass

    async def force_alive_check(self) -> bool:
        """Trigger a roll call immediately (used by /hell alivecheck)."""
        if not self.engine.is_running or self.alive_checks.pending is not None:
            return False
        collected = await self.collect()
        if collected is None or not collected[0]:
            return False
        async with self._lock:
            check = await self.alive_checks.start(now_ts(), collected[0])
        return check is not None

    # -------------------------------------------------------------- recovery

    async def resume_after_restart(self) -> None:
        """Bring an event back to life (or finish its paperwork) after a restart."""
        state = self.engine.state
        if state.status.is_terminal:
            # The run ended before/while the bot was down: finish sending the
            # stat cards that never went out.
            if self.reports.pending():
                log.info("Resuming end-of-event stat cards after restart")
                self.reports.schedule()
            return
        if state.status is not EventStatus.RUNNING:
            return
        gap = now_ts() - (state.last_tick_ts or state.start_ts or now_ts())
        log.info(
            "Resuming event %s — elapsed %s, unobserved gap %.0fs",
            state.event_uid,
            format_hm(self.engine.elapsed()),
            gap,
        )
        # A roll call interrupted by the restart is cancelled, never enforced:
        # nobody gets disconnected because the bot was offline.
        self.alive_checks.bind(state.event_uid)
        pending = self.alive_checks.pending
        if pending is not None:
            recovered = await self.alive_checks.backfill_replies()
            if now_ts() >= pending.deadline_ts:
                log.warning("Alive check %s expired while offline — cancelling it", pending.check_id)
                await self.alive_checks.cancel(now_ts(), "the bot restarted while it was running")
            else:
                log.info(
                    "Resuming alive check %s (%d reply(ies) recovered, %.0fs left)",
                    pending.check_id,
                    recovered,
                    pending.seconds_left(now_ts()),
                )

        # Any milestone claimed but never announced (crash between the two).
        pending = self.engine.pending_announcements()
        if pending:
            log.warning("Re-announcing %d milestone(s) that were never posted", len(pending))
            await self.announcer.announce_pending(pending)
        self.announcer.forget_progress_message()
        self._terminal_rendered = False
        await self.announcer.update_progress(self.engine.snapshot(), force=True)
