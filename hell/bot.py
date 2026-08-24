"""Welcome to Hell — bot entrypoint.

    python bot.py                 # console
    python -m hell.bot            # same
    launcher (GUI)                # see launcher/ and run_bot.bat

Wiring:
    Store (persistence) -> HellEngine (state / tracking / milestones)
                        -> Announcer (messages) -> VoiceMonitor (loops)
                        -> HellCommands (slash commands)
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import sys
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from . import RESTART_EXIT_CODE, __version__
from .announcer import Announcer
from .cog import HellCommands
from .config import Config, ConfigError
from .engine import HellEngine
from .health import HealthReport, preflight
from .logging_setup import setup_logging
from .logsink import DiscordLogStream
from .monitor import VoiceMonitor
from .storage import Store
from .tasks import spawn as _spawn
from .texts import TEXT
from .texts import source as texts_source
from .timeutil import format_hm, format_hms, now_ts
from .web import broadcast, start_server, stop_server

log = logging.getLogger("hell")


def _active_tasks_count() -> int:
    """Count running background tasks (import-safe helper)."""
    from .tasks import active
    return active()


class HellBot(commands.Bot):
    """The Discord client with every subsystem attached."""

    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True        # required to read VC members and their roles
        intents.voice_states = True   # required to see who is in the VC
        intents.guilds = True
        # Required to READ message content: alive-check replies ("Yes") are
        # ordinary chat messages that never mention the bot — without this
        # intent Discord delivers an empty content and no one can ever answer
        # a roll call.  It is a privileged intent: enable "Message Content
        # Intent" in the Developer Portal or login fails (PrivilegedIntents).
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents, help_command=None)

        self.config = config
        self.store = Store(config.database_path)
        self.engine = HellEngine(self.store, config)
        self.announcer = Announcer(self, config, self.engine)
        self.monitor = VoiceMonitor(self, config, self.engine, self.announcer)
        # Live log stream: attach immediately so records produced during
        # startup are buffered and delivered as soon as the DM opens.
        self.log_stream = DiscordLogStream(self, config)
        self.log_stream.attach()
        self.health: Optional[HealthReport] = None
        self._resumed = False
        self._status_task: Optional[asyncio.Task] = None
        self._web_runner: Optional[aiohttp.web.AppRunner] = None
        self._web_push_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        await self.add_cog(HellCommands(self, self.config, self.engine, self.monitor))
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash command(s) to guild %s", len(synced), self.config.guild_id)
        except discord.HTTPException as exc:
            log.error("Could not sync slash commands: %s", exc)

    async def on_ready(self) -> None:
        log.info("Welcome to Hell v%s — logged in as %s (%s)", __version__, self.user,
                 getattr(self.user, "id", "?"))
        for label, value in self.config.summary():
            log.info("  %-20s %s", label + ":", value)
        log.info(
            "Event status: %s | elapsed %s | messages from %s",
            self.engine.status.value,
            format_hm(self.engine.elapsed()),
            texts_source(),
        )

        try:
            self.health = await preflight(self, self.config)
            self.health.log()
        except Exception:  # pragma: no cover - never die on a check
            log.exception("Preflight checks failed to run")

        await self._update_presence()
        self._start_status_rotation()
        # Start the live web dashboard.
        if self.config.web_port > 0:
            self._web_runner = await start_server(self.config.web_port)
            self._web_push_task = _spawn(self._web_push_loop(), name="web-push")
        try:
            await self.log_stream.start()
        except Exception:  # pragma: no cover - streaming must never block boot
            log.exception("Could not start the live log stream")
        self.monitor.start()
        self.monitor.sync_status()
        if not self._resumed:
            self._resumed = True
            try:
                await self.monitor.resume_after_restart()
            except Exception:  # pragma: no cover - never die on recovery
                log.exception("Recovery after restart failed")

    async def on_resumed(self) -> None:
        log.info("Gateway session resumed")

    async def on_disconnect(self) -> None:
        log.warning("Disconnected from the gateway (will auto-reconnect)")

    async def _update_presence(self) -> None:
        """Initial status and start the rotating status loop."""
        try:
            if self.engine.is_running:
                text = f"Hell: {format_hm(self.engine.elapsed())} / 160h"
            elif self.engine.status.value == "COMPLETED":
                text = "Hell conquered — 160h"
            elif self.engine.status.value == "FAILED":
                text = "Hell failed — /hell status"
            else:
                text = "/hell start"
            await self.change_presence(activity=discord.CustomActivity(name=text[:128]))
        except Exception:  # pragma: no cover - cosmetic only
            log.debug("Could not update presence", exc_info=True)

    def _start_status_rotation(self) -> None:
        """Start the rotating status loop."""
        if self._status_task is not None and not self._status_task.done():
            self._status_task.cancel()
        self._status_task = _spawn(self._status_rotation_loop(), name="status-rotation")

    async def _status_rotation_loop(self) -> None:
        """Rotate the bot's Discord status every 60 seconds with live event info."""
        rng = random.Random()
        while not self.is_closed():
            try:
                await self._set_rotating_status(rng)
            except Exception:
                log.debug("Could not update rotating status", exc_info=True)
            await asyncio.sleep(60)

    async def _set_rotating_status(self, rng: random.Random) -> None:
        """Pick one status message from the pool and apply it."""
        if not self.engine.is_running:
            if self.engine.status.value == "COMPLETED":
                text = "🏆 Hell conquered — 160h"
            elif self.engine.status.value == "FAILED":
                text = "💀 Hell failed — /hell status"
            else:
                text = "💤 /hell start"
            await self.change_presence(activity=discord.CustomActivity(name=text[:128]))
            return

        snap = self.engine.snapshot()
        board = self.engine.leaderboard()
        alive = self.monitor.alive_checks.pending
        vc_count = snap.participants

        options: list[str] = []

        # 1) #1 on the leaderboard
        if board:
            top = board[0]
            options.append(f"🥇 {top.display_name} — {format_hms(top.seconds)}")

        # 2) Elapsed time
        options.append(f"⏱️ {format_hm(snap.elapsed)} / 160h ({snap.fraction * 100:.1f}%)")

        # 3) Time remaining
        options.append(f"⏳ {format_hm(snap.remaining)} remaining")

        # 4) People in VC
        options.append(f"👥 {vc_count} in Hell right now")

        # 5) Current / next milestone
        if snap.current:
            if snap.upcoming and snap.time_to_next is not None:
                options.append(
                    f"🔥 {snap.current.hours}h cleared → next: {snap.upcoming.hours}h in {format_hm(snap.time_to_next)}"
                )
            else:
                options.append("🏆 All milestones cleared!")

        # 6) Alive check status
        if alive is not None:
            answered = len(alive.responded)
            total = len(alive.required)
            left = max(0, int(alive.deadline_ts - now_ts()))
            options.append(f"🚨 Alive check: {answered}/{total} answered, {left}s left")

        # 7) Progress bar
        from .timeutil import milestone_bar
        bar = milestone_bar(snap.fraction)
        options.append(f"📊 {bar}")

        text = rng.choice(options) if options else "🔥 Welcome to Hell"
        await self.change_presence(activity=discord.CustomActivity(name=text[:128]))

    async def _web_push_loop(self) -> None:
        """Push live data to all connected WebSocket browsers every 5 seconds."""
        while not self.is_closed():
            try:
                await self._push_web_snapshot()
            except Exception:
                log.debug("Web push failed", exc_info=True)
            await asyncio.sleep(5)

    async def _push_web_snapshot(self) -> None:
        """Build and broadcast the current state to every browser tab."""
        from .web import client_count
        if client_count() == 0:
            return

        now = now_ts()
        snap = self.engine.snapshot(now=now)
        board = self.engine.leaderboard()

        # Leaderboard (top 20 for the web).
        lb: list[dict] = []
        for e in board[:20]:
            lb.append({
                "rank": e.rank,
                "name": e.display_name,
                "seconds": round(e.seconds, 1),
                "time": format_hms(e.seconds),
            })

        # Alive check.
        alive_info: dict | None = None
        ac = self.monitor.alive_checks.pending
        if ac is not None:
            alive_info = {
                "answered": len(ac.responded),
                "total": len(ac.required),
                "seconds_left": max(0, int(ac.deadline_ts - now)),
            }

        # Milestones.
        current_ms = snap.current
        upcoming_ms = snap.upcoming

        from .milestones import MILESTONES
        milestone_records_map = {r.hours: r for r in self.engine.milestone_records()}
        milestones_list: list[dict] = []
        for m in MILESTONES:
            rec = milestone_records_map.get(m.hours)
            reached = rec is not None or (snap.elapsed >= m.seconds)
            time_to = max(0.0, m.seconds - snap.elapsed) if not reached else 0.0
            milestones_list.append({
                "hours": m.hours,
                "title": m.title,
                "reward": m.reward,
                "short_reward": m.short_reward or m.reward,
                "blurb": m.blurb,
                "reached": reached,
                "reached_ts": rec.reached_ts if rec else None,
                "members_count": len(rec.members) if rec else 0,
                "time_to": round(time_to, 1) if (self.engine.status.is_active and not reached) else None,
            })

        estimated_end_ts: Optional[float] = None
        if snap.start_ts is not None:
            if self.engine.status.is_active:
                estimated_end_ts = snap.start_ts + snap.total + self.engine.state.paused_seconds
            elif snap.end_ts is not None:
                estimated_end_ts = snap.end_ts

        # Health / errors / warnings.
        health_errors: list[str] = []
        health_warnings: list[str] = []
        health_info: list[str] = []
        if self.health:
            health_errors = list(self.health.errors)
            health_warnings = list(self.health.warnings)
            health_info = list(self.health.info)

        # Log tail (last 50 lines).
        log_tail: list[str] = []
        stream = getattr(self, "log_stream", None)
        if stream is not None:
            log_tail = stream.tail(50)

        # Security snapshot.
        sec = self.monitor.security.snapshot()

        payload = {
            "type": "snapshot",
            "ts": now,
            # --- event stats ---
            "status": snap.status.value if snap.status else "IDLE",
            "elapsed": round(snap.elapsed, 1),
            "total": round(snap.total, 1),
            "remaining": round(snap.remaining, 1),
            "fraction": round(snap.fraction, 4),
            "participants": snap.participants,
            "start_ts": snap.start_ts,
            "end_ts": snap.end_ts,
            "estimated_end_ts": estimated_end_ts,
            "paused": snap.paused,
            "pause_reason": self.engine.state.pause_reason,
            "end_reason": self.engine.state.end_reason,
            "grace_open": snap.grace_open,
            "grace_seconds_left": round(snap.grace_seconds_left, 1),
            "grace_total": round(snap.grace_total, 1),
            # --- milestones ---
            "current_milestone": {
                "hours": current_ms.hours,
                "title": current_ms.title,
                "short_reward": current_ms.short_reward or current_ms.reward,
            } if current_ms else None,
            "upcoming_milestone": {
                "hours": upcoming_ms.hours,
                "title": upcoming_ms.title,
                "short_reward": upcoming_ms.short_reward or upcoming_ms.reward,
                "time_to": round(snap.time_to_next, 1) if snap.time_to_next is not None else None,
            } if upcoming_ms else None,
            "milestones": milestones_list,
            # --- leaderboard ---
            "leaderboard": lb,
            "leaderboard_total": len(board),
            # --- alive check ---
            "alive_check": alive_info,
            # --- dev data ---
            "health_errors": health_errors,
            "health_warnings": health_warnings,
            "health_info": health_info,
            "log_tail": log_tail,
            "rate_limits_5min": sec.get("rate_limits_5min", 0),
            "monitor_stale_seconds": sec.get("stale_seconds"),
            "blind_seconds": round(self.monitor.blind_seconds, 1),
            "active_tasks": _active_tasks_count(),
            "voice_channel_id": self.engine.state.voice_channel_id or self.config.voice_channel_id,
            "guild_id": self.engine.state.guild_id or self.config.guild_id,
            "operator_dm_ok": stream.enabled if stream else True,
            "version": __version__,
        }
        await broadcast(payload)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Fast path: kick `@clanker` users and force-kicked bots the instant they join the target VC.

        The 1-second loop would catch them anyway; this just makes it immediate.
        """
        cid = self.config.voice_channel_id
        if after.channel is None or after.channel.id != cid:
            return
        # Force-kick specific bots that must never stay in the VC.
        if member.bot:
            if member.id in self.config.kick_bot_ids:
                await self.monitor.kick_clankers([member])
            return
        if self.monitor.is_clanker(member):
            await self.monitor.kick_clankers([member])

    async def on_message(self, message: discord.Message) -> None:
        """Handle alive-check answers in guild channels and ! prefix commands strictly in DMs."""
        if message.author.bot:
            return
        if message.guild is not None:
            try:
                await self.monitor.handle_message(message)
            except Exception:  # pragma: no cover - never break on a chat message
                log.exception("Failed to handle a message for the alive check")
            return
        # Direct Messages (DMs) only: process ! prefix commands
        await self.process_commands(message)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if ctx.guild is not None:
            return
        if isinstance(error, commands.CommandNotFound):
            await ctx.send("Unknown command. Type `!help` or `!status` for a list of available commands.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing required argument `{error.param.name}`. Type `!help` for usage.")
            return
        if isinstance(error, (commands.CheckFailure, commands.NotOwner)):
            await ctx.send(TEXT.CMD_NOT_A_HOST)
            return
        log.exception("Command error in %s: %s", getattr(ctx.command, "name", "?"), error)
        try:
            await ctx.send(TEXT.CMD_ERROR)
        except Exception:
            pass

    async def on_error(self, event_method: str, *args, **kwargs) -> None:  # pragma: no cover
        log.exception("Unhandled exception in %s", event_method)

    async def close(self) -> None:
        log.info("Shutting down…")
        self.monitor.stop()
        if self._web_push_task is not None:
            self._web_push_task.cancel()
        try:
            await stop_server(self._web_runner)
        except Exception:  # pragma: no cover
            pass
        try:
            await self.log_stream.stop()
        except Exception:  # pragma: no cover
            pass
        try:
            await super().close()
        finally:
            self.store.close()
            log.info("Shutdown complete")


def build_bot(config: Config) -> HellBot:
    return HellBot(config)


async def run(config: Optional[Config] = None) -> None:
    if config is None:
        config = Config.from_env()
    setup_logging(config.log_level)
    bot = build_bot(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except (NotImplementedError, RuntimeError, AttributeError):  # Windows / non-main thread
            pass

    async with bot:
        await bot.start(config.token)


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Configuration error: %s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in (or use the launcher).", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        asyncio.run(run(config))
    except SystemExit as exc:
        if exc.code == RESTART_EXIT_CODE:
            raise  # let the restart signal propagate to the launcher / Docker
        # Other exit codes (normal shutdown, config errors) are handled below.
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except discord.LoginFailure:
        _fail(3, "Discord rejected the token — check DISCORD_TOKEN in your .env")
    except discord.PrivilegedIntentsRequired:
        _fail(
            4,
            "The Server Members intent is not enabled for this application. Enable it at "
            "Developer Portal -> Bot -> Privileged Gateway Intents, then start the bot again.",
        )
    except (aiohttp.ClientConnectorError, OSError) as exc:
        # No internet, DNS failure, blocked egress… a stack trace helps nobody.
        log.error("Could not reach Discord: %s", exc)
        _fail(5, "Could not reach Discord. Check the internet connection and try again.")
    except Exception as exc:
        log.exception("The bot stopped with an unexpected error")
        _fail(1, f"Unexpected error: {type(exc).__name__}: {exc} (full traceback in logs/)")


def _fail(code: int, message: str) -> None:
    log.error("%s", message)
    print(message, file=sys.stderr)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
