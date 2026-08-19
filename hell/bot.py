"""Welcome to Hell — bot entrypoint.

    python -m hell.bot        (or)        python bot.py

Wires the subsystems together:
    Store (persistence) -> HellEngine (state/tracking/milestones)
                        -> Announcer (messages)  -> VoiceMonitor (loops)
                        -> HellCommands (slash commands)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from .announcer import Announcer
from .cog import HellCommands
from .config import Config, ConfigError
from .engine import HellEngine
from .monitor import VoiceMonitor
from .storage import Store

log = logging.getLogger("hell")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


class HellBot(commands.Bot):
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
        self._resumed = False

    async def setup_hook(self) -> None:
        await self.add_cog(HellCommands(self, self.config, self.engine, self.monitor))
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash command(s) to guild %s", len(synced), self.config.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        log.info(
            "Event status: %s | elapsed %.0fs | VC %s | announcements #%s",
            self.engine.status.value,
            self.engine.elapsed(),
            self.config.voice_channel_id,
            self.config.announce_channel_id,
        )
        self.monitor.start()
        if not self._resumed:
            self._resumed = True
            try:
                await self.monitor.resume_after_restart()
            except Exception:  # pragma: no cover - never die on recovery
                log.exception("Recovery after restart failed")

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

    async def close(self) -> None:
        self.monitor.stop()
        try:
            await super().close()
        finally:
            self.store.close()


async def run() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        raise SystemExit(2)

    setup_logging(config.log_level)
    bot = HellBot(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    async with bot:
        await bot.start(config.token)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
