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
import signal
import sys
from typing import Optional

import discord
from discord.ext import commands

from .announcer import Announcer
from .cog import HellCommands
from .config import Config, ConfigError
from .engine import HellEngine
from .health import HealthReport, preflight
from .logging_setup import setup_logging
from .monitor import VoiceMonitor
from .storage import Store

log = logging.getLogger("hell")


class HellBot(commands.Bot):
    """The Discord client with every subsystem attached."""

    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True        # required to read VC members and their roles
        intents.voice_states = True   # required to see who is in the VC
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.store = Store(config.database_path)
        self.engine = HellEngine(self.store, config)
        self.announcer = Announcer(self, config, self.engine)
        self.monitor = VoiceMonitor(self, config, self.engine, self.announcer)
        self.health: Optional[HealthReport] = None
        self._resumed = False

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
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        log.info(
            "Event status: %s | elapsed %.0fs | VC %s | announcements #%s | db %s",
            self.engine.status.value,
            self.engine.elapsed(),
            self.config.voice_channel_id,
            self.config.announce_channel_id,
            self.config.database_path,
        )

        try:
            self.health = await preflight(self, self.config)
            self.health.log()
        except Exception:  # pragma: no cover - never die on a check
            log.exception("Preflight checks failed to run")

        await self._update_presence()
        self.monitor.start()
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
        """Show the event state in the bot's Discord status."""
        try:
            from .timeutil import format_hm

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

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Fast path: kick `@clanker` users the instant they join the target VC.

        The 1-second loop would catch them anyway; this just makes it immediate.
        """
        cid = self.config.voice_channel_id
        if member.bot or after.channel is None or after.channel.id != cid:
            return
        if self.monitor.is_clanker(member):
            await self.monitor.kick_clankers([member])

    async def on_message(self, message: discord.Message) -> None:
        """Alive-check answers arrive as ordinary chat messages."""
        if message.guild is None or message.author.bot:
            return
        try:
            await self.monitor.handle_message(message)
        except Exception:  # pragma: no cover - never break on a chat message
            log.exception("Failed to handle a message for the alive check")
        await self.process_commands(message)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:  # pragma: no cover
        log.exception("Unhandled exception in %s", event_method)

    async def close(self) -> None:
        log.info("Shutting down…")
        self.monitor.stop()
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
        raise SystemExit(2)

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except discord.LoginFailure:
        log.error("Discord rejected the token — check DISCORD_TOKEN in your .env")
        raise SystemExit(3)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "The Server Members intent is not enabled for this application. "
            "Enable it at Developer Portal -> Bot -> Privileged Gateway Intents."
        )
        raise SystemExit(4)


if __name__ == "__main__":
    main()
