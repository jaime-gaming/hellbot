"""Voice channel monitoring.

Runs two loops:

* **1 second** — read the target VC, drop bots, kick `@clanker` users, feed a
  trusted :class:`~hell.engine.Observation` into the engine and dispatch any
  domain events (milestone / failure / completion) to the announcer.
* **`PROGRESS_INTERVAL` seconds** (20 by default) — edit the live progress
  message. Editing less often keeps well clear of Discord's edit rate limits.

An observation is only fed to the engine when it can be *trusted*: the gateway
is connected, the guild and the voice channel resolved, and the startup grace
window has elapsed.  Otherwise the bot would risk failing the event just
because its cache was cold after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Optional

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
from .security import SuspicionTracker
from .status_writer import get_status as get_status_writer
from .tasks import spawn
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
        self.security = SuspicionTracker(self.alive_checks)
        self._ready_at: Optional[float] = None
        self._kick_attempts: dict[int, float] = {}
        self._lock = asyncio.Lock()  # serialises ticks; no milestone can race
        self._known_presence: dict[int, str] = {}
        self._alive_task: Optional[asyncio.Task] = None
        self._blind_since: Optional[float] = None
        self._blind_logged = False
        self._last_heartbeat = 0.0
        self._terminal_rendered = False
        self._monitor_loop.change_interval(seconds=max(0.25, config.monitor_interval))
        self._progress_loop.change_interval(seconds=max(5.0, config.progress_interval))

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        log.info(
            "Monitoring VC %s every %.0fs; progress message every %.0fs",
            self.config.voice_channel_id,
            self.config.monitor_interval,
            self.config.progress_interval,
        )
        self._ready_at = now_ts()
        if not self._monitor_loop.is_running():
            self._monitor_loop.start()
        if not self._progress_loop.is_running():
            self._progress_loop.start()

    def stop(self) -> None:
        log.info("Stopping the VC monitor")
        self._monitor_loop.cancel()
        self._progress_loop.cancel()
        if self._alive_task is not None and not self._alive_task.done():
            self._alive_task.cancel()

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
        self.security.record_collect()

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

    def _log_presence_changes(self, humans: Sequence[ParticipantRef]) -> None:
        """Emit a log line whenever somebody joins or leaves the target VC.

        These lines are what makes the operator's live log stream readable —
        they go to the file log, the launcher console and the DM stream alike.
        """
        current = {p.user_id: p.display_name for p in humans}
        joined = [current[uid] for uid in current if uid not in self._known_presence]
        left = [name for uid, name in self._known_presence.items() if uid not in current]
        if joined or left:
            for name in joined:
                log.info("➕ %s joined the VC (%d valid human(s) inside)", name, len(current))
            for name in left:
                log.info("➖ %s left the VC (%d valid human(s) inside)", name, len(current))
        # Feed join/leave to the security tracker for flap detection and
        # alive-check dodging analysis.
        for uid in [uid for uid in current if uid not in self._known_presence]:
            self.security.record_join(uid)
        for uid in [uid for uid in self._known_presence if uid not in current]:
            self.security.record_leave(uid)
            if self.alive_checks.pending is not None:
                self.security.record_check_departure(uid, before_check=True)
        self._known_presence = current

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
        self._log_presence_changes(humans)
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

        # While paused the event is frozen: no roll calls may start or resolve
        # (nobody should be kicked for failing to answer during a freeze).
        if self.engine.is_running and not self.engine.is_paused:
            self._ensure_alive_checks_bound(now)
            self._pump_alive_check(now, humans)
        self._heartbeat(len(humans))
        # Security/anomaly checks (run on every tick, cheap).
        self.security.check_stale()
        self.security.check_rate_limit_spike()

    def _ensure_alive_checks_bound(self, now: float) -> None:
        """Keep the roll-call manager attached to the current event.

        Binding normally happens in `/hell start` and on restart recovery. If
        anything ever skips that (a new event created another way, a manual
        reset), an unbound manager would silently never run a check again —
        so the monitor re-binds it here instead of trusting the caller.
        """
        if self.alive_checks.bound_uid != self.engine.event_uid:
            log.info("Binding alive checks to event %s", self.engine.event_uid)
            self.alive_checks.bind(self.engine.event_uid, now=now)

    def _pump_alive_check(self, now: float, humans: Sequence[ParticipantRef]) -> None:
        """Advance the roll call **off** the monitor's critical path.

        Posting a roll call and disconnecting silent members are network calls
        that can take seconds. Running them inline would stall the 1-second VC
        loop, delaying empty-VC detection and costing people leaderboard time,
        so they run in their own task while the monitor keeps ticking.
        """
        if self._alive_task is not None and not self._alive_task.done():
            return  # a previous roll call step is still in flight

        async def runner() -> None:
            try:
                await self.alive_checks.tick(now, list(humans))
            except Exception:  # pragma: no cover - never break the event loop
                log.exception("Alive check tick failed")

        self._alive_task = spawn(runner(), name="alive-check")

    def _heartbeat(self, participants: int) -> None:
        """Periodic proof-of-life in the log file, useful when running headless."""
        interval = max(60.0, self.config.heartbeat_minutes * 60.0)
        now = now_ts()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        snap = self.engine.snapshot(now=now, participants=participants)
        status = "⏸️ PAUSED" if snap.paused else snap.status.value
        log.info(
            "Heartbeat: %s | %s / %s (%.1f%%) | %d in VC | next milestone: %s",
            status,
            format_hm(snap.elapsed),
            format_hm(snap.total),
            snap.fraction * 100,
            participants,
            f"{snap.upcoming.hours}h" if snap.upcoming else "none",
        )
        # Write the GitHub Pages status JSON on every heartbeat.
        try:
            get_status_writer().write("docs/status.json", engine=self.engine, monitor=self, stream=getattr(self.bot, "log_stream", None))
        except Exception:
            log.debug("Could not write status.json", exc_info=True)

    @tasks.loop(seconds=20.0)
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
        if not self.engine.is_running or self.engine.is_paused:
            return False
        if self.alive_checks.pending is not None:
            return False
        self._ensure_alive_checks_bound(now_ts())
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
        if self.engine.is_paused:
            # The event was frozen before the restart and stays frozen: no
            # recovery work (roll calls, re-announcements) happens while
            # paused — `/hell resume` handles the unpause.
            log.warning("Event %s is PAUSED — restarting in frozen state", state.event_uid)
            return
        # Reconstruct the real-time observation timestamp: the effective
        # last_tick_ts is pause-adjusted, so computing the gap against it
        # would include the paused duration — leading to misleading log
        # messages and over-10s announcements.
        gap_raw = now_ts() - (state.last_tick_ts or state.start_ts or now_ts())
        gap_raw = max(0.0, gap_raw)
        real_last = (state.start_ts or now_ts()) + self.engine.elapsed() + state.paused_seconds
        gap = max(0.0, now_ts() - real_last)
        log.info(
            "Resuming event %s — elapsed %s, unobserved gap %.0fs (raw %.0fs)",
            state.event_uid,
            format_hm(self.engine.elapsed()),
            gap, gap_raw,
        )
        # A roll call interrupted by the restart is cancelled, never enforced:
        # nobody gets disconnected because the bot was offline.
        self.alive_checks.bind(state.event_uid)
        pending_check = self.alive_checks.pending
        if pending_check is not None:
            recovered = await self.alive_checks.backfill_replies()
            if now_ts() >= pending_check.deadline_ts:
                log.warning(
                    "Alive check %s expired while offline — cancelling it", pending_check.check_id
                )
                await self.alive_checks.cancel(now_ts(), "the bot restarted while it was running")
            else:
                log.info(
                    "Resuming alive check %s (%d reply(ies) recovered, %.0fs left)",
                    pending_check.check_id,
                    recovered,
                    pending_check.seconds_left(now_ts()),
                )

        # Any milestone claimed but never announced (crash between the two).
        unannounced = self.engine.pending_announcements()
        if unannounced:
            log.warning("Re-announcing %d milestone(s) that were never posted", len(unannounced))
            await self.announcer.announce_pending(unannounced)
        self.announcer.forget_progress_message()
        self._terminal_rendered = False
        await self.announcer.update_progress(self.engine.snapshot(), force=True)

        # Let the guild know the bot recovered from a restart.
        if gap > 10.0:
            embed = discord.Embed(
                title="🔄 Bot restart recovered",
                description=(
                    f"The bot was restarted and is back online. "
                    f"The event was unobserved for **{gap:.0f}s** — "
                    f"the timer never stopped and nobody was penalised. "
                    f"Elapsed: **{format_hm(self.engine.elapsed())}** / 160h."
                ),
                color=0xE25822,
            )
            try:
                await self.announcer.send([embed])
            except Exception:
                log.exception("Could not post the restart-recovery announcement")
