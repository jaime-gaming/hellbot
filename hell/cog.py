"""Discord command handling — the `/hell` slash command group."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .engine import HellEngine, StartError
from .milestones import MILESTONES, TOTAL_SECONDS
from .models import EventStatus
from .monitor import VoiceMonitor
from .texts import TEXT, say
from .texts import message_count, reload as reload_texts, source as texts_source
from .timeutil import discord_ts, format_hm, now_ts

log = logging.getLogger("hell.commands")

RESET_PHRASE = "RESET WELCOME TO HELL"


class NotAHost(app_commands.CheckFailure):
    """Raised when a non-host tries to run a restricted command."""


def is_host():
    """Restrict a command to members holding the `@gamenight host` role."""

    async def predicate(interaction: discord.Interaction) -> bool:
        config: Config = interaction.client.config  # type: ignore[attr-defined]
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise NotAHost("This command can only be used inside the server.")
        if not any(r.id == config.gamenight_host_role_id for r in member.roles):
            raise NotAHost(
                say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{config.gamenight_host_role_id}>")
            )
        return True

    return app_commands.check(predicate)


class ConfirmView(discord.ui.View):
    """Yes/no confirmation restricted to the invoking host."""

    def __init__(self, author_id: int, *, confirm_label: str = "Confirm", timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This confirmation is not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()


class ResetModal(discord.ui.Modal, title="Reset Welcome to Hell"):
    """Strong confirmation: the host must type the exact phrase."""

    phrase: discord.ui.TextInput = discord.ui.TextInput(
        label=f'Type "{RESET_PHRASE}" to confirm',
        placeholder=RESET_PHRASE,
        required=True,
        max_length=64,
    )

    def __init__(self, cog: "HellCommands"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.phrase.value).strip() != RESET_PHRASE:
            await interaction.response.send_message(TEXT.CMD_RESET_MISMATCH, ephemeral=True)
            return
        engine = self.cog.engine
        was = engine.status
        async with self.cog.monitor.lock:
            engine.reset()
            self.cog.monitor.alive_checks.reset()
        self.cog.announcer.forget_progress_message()
        log.warning("Event data reset by %s (previous status: %s)", interaction.user, was.value)
        await interaction.response.send_message(
            say(TEXT.CMD_RESET_DONE, previous_status=was.value), ephemeral=False
        )


class HellCommands(commands.GroupCog, name="hell", description="Welcome to Hell event controls"):
    """`/hell start`, `/hell status`, `/hell leaderboard`, `/hell stop`, `/hell reset`."""

    def __init__(self, bot: commands.Bot, config: Config, engine: HellEngine, monitor: VoiceMonitor):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.monitor = monitor
        self.announcer = monitor.announcer
        super().__init__()

    # ----------------------------------------------------------------- start

    @app_commands.command(name="start", description="Start Welcome to Hell (160h). Requires @gamenight host.")
    @is_host()
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        if self.engine.is_running:
            elapsed = self.engine.elapsed()
            await interaction.followup.send(
                say(TEXT.CMD_ALREADY_RUNNING, elapsed=format_hm(elapsed)), ephemeral=True
            )
            return

        if self.engine.status.is_terminal and not self.engine.state.final_saved:
            self.engine.freeze_leaderboard()

        collected = await self.monitor.collect()
        if collected is None:
            await interaction.followup.send(
                say(TEXT.CMD_VC_UNREACHABLE, vc=f"<#{self.config.voice_channel_id}>"),
                ephemeral=True,
            )
            return
        humans, clankers = collected
        if clankers:
            await self.monitor.kick_clankers(clankers)
        if self.config.require_occupants_to_start and not humans:
            await interaction.followup.send(
                say(TEXT.CMD_VC_EMPTY_ON_START, vc=f"<#{self.config.voice_channel_id}>"),
                ephemeral=True,
            )
            return

        assert interaction.guild is not None
        try:
            async with self.monitor.lock:  # never race the 1s monitor tick
                if self.engine.is_running:
                    raise StartError("An event is already RUNNING.")
                self.engine.start(
                    now=now_ts(),
                    guild_id=interaction.guild.id,
                    voice_channel_id=self.config.voice_channel_id,
                    announce_channel_id=self.config.announce_channel_id,
                    started_by=interaction.user.id,
                    initial_participants=humans,
                )
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # Schedule the first roll call 1-6 hours from now.
        self.monitor.alive_checks.bind(self.engine.event_uid, now=now_ts())

        self.announcer.forget_progress_message()
        snap = self.engine.snapshot(participants=len(humans))
        await self.announcer.announce_start(snap, interaction.user, humans)
        await self.announcer.update_progress(snap)
        await interaction.followup.send(
            say(
                TEXT.CMD_STARTED,
                started_at=discord_ts(snap.start_ts or 0, "T"),
                total=format_hm(TOTAL_SECONDS),
                vc=f"<#{self.config.voice_channel_id}>",
                announce_channel=f"<#{self.config.announce_channel_id}>",
            ),
            ephemeral=True,
        )

    # ---------------------------------------------------------------- status

    @app_commands.command(name="status", description="Show the current Welcome to Hell status.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        if self.engine.status is EventStatus.IDLE:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=TEXT.CMD_IDLE_TITLE,
                    description=say(
                        TEXT.CMD_IDLE_TEXT,
                        host_role=f"<@&{self.config.gamenight_host_role_id}>",
                        vc=f"<#{self.config.voice_channel_id}>",
                    ),
                    color=int(TEXT.COLOR_IDLE),
                )
            )
            return

        count = len(self.engine.last_participants)
        if self.engine.is_running:
            collected = await self.monitor.collect()
            if collected is not None:
                count = len(collected[0])
        snap = self.engine.snapshot(participants=count)
        alive_line = self.monitor.alive_checks.status_line(now_ts()) if self.engine.is_running else None
        await interaction.followup.send(
            embed=self.announcer.build_status(snap, alive_line=alive_line)
        )

    # ----------------------------------------------------------- leaderboard

    @app_commands.command(name="leaderboard", description="Show the Welcome to Hell leaderboard.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        entries = self.engine.leaderboard()
        frozen = self.engine.status.is_terminal
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LEADERBOARD"
        embeds = self.announcer.build_leaderboard_embeds(entries, title=title)
        if frozen and embeds:
            embeds[-1].set_footer(text="These rankings are frozen; the event is over.")
        await interaction.followup.send(embeds=embeds)

    # ------------------------------------------------------------ milestones

    @app_commands.command(name="milestones", description="Show every milestone, its reward and who claimed it.")
    @app_commands.guild_only()
    async def milestones(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        records = {r.hours: r for r in self.engine.milestone_records()}
        elapsed = self.engine.elapsed()
        embed = discord.Embed(
            title=TEXT.CMD_MILESTONES_TITLE,
            description=TEXT.CMD_MILESTONES_DESCRIPTION,
            color=int(TEXT.COLOR_MILESTONE),
        )
        for m in MILESTONES:
            record = records.get(m.hours)
            if record:
                state = say(
                    TEXT.CMD_MILESTONES_REACHED,
                    reached_at=discord_ts(record.reached_ts, "f"),
                    member_count=len(record.members),
                )
            elif self.engine.is_running:
                state = say(
                    TEXT.CMD_MILESTONES_PENDING,
                    time_to_go=format_hm(max(0.0, m.seconds - elapsed)),
                )
            else:
                state = TEXT.CMD_MILESTONES_IDLE
            embed.add_field(
                name=say(
                    TEXT.CMD_MILESTONES_FIELD,
                    hours=m.hours,
                    short_reward=m.short_reward or m.reward,
                ),
                value=f"{self.config.reward_text(m.hours, m.reward)}\n{state}",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------- log stream

    @app_commands.command(
        name="logs",
        description="Control the live log stream that is DM'd to the operator.",
    )
    @app_commands.describe(
        action="Turn the stream on/off, show its status, or send a test line.",
        level="Minimum severity mirrored to the DM stream.",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="status", value="status"),
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="test", value="test"),
            app_commands.Choice(name="flush", value="flush"),
        ],
        level=[
            app_commands.Choice(name="DEBUG (everything)", value="DEBUG"),
            app_commands.Choice(name="INFO (joins, leaves, milestones)", value="INFO"),
            app_commands.Choice(name="WARNING (problems only)", value="WARNING"),
            app_commands.Choice(name="ERROR (failures only)", value="ERROR"),
        ],
    )
    @is_host()
    @app_commands.guild_only()
    async def logs(
        self,
        interaction: discord.Interaction,
        action: Optional[app_commands.Choice[str]] = None,
        level: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        stream = getattr(self.bot, "log_stream", None)
        if stream is None:
            await interaction.followup.send(TEXT.CMD_LOGS_UNAVAILABLE, ephemeral=True)
            return

        choice = action.value if action else "status"
        if level is not None:
            stream.set_level(level.value)
            log.info("Live log level set to %s by %s", level.value, interaction.user)

        if choice == "on":
            stream.set_enabled(True)
            if not stream.running:
                await stream.start()
            message = TEXT.CMD_LOGS_ON
        elif choice == "off":
            stream.set_enabled(False)
            message = TEXT.CMD_LOGS_OFF
        elif choice == "test":
            log.warning("Live log test triggered by %s (%s)", interaction.user, interaction.user.id)
            await stream.flush()
            message = TEXT.CMD_LOGS_TEST
        elif choice == "flush":
            sent = await stream.flush()
            message = say(TEXT.CMD_LOGS_FLUSHED, sent=sent)
        else:
            message = say(TEXT.CMD_LOGS_STATUS, status=stream.status())

        await interaction.followup.send(message, ephemeral=True)

    # --------------------------------------------------------------- my stats

    @app_commands.command(name="mystats", description="Your personal Welcome to Hell stat card.")
    @app_commands.guild_only()
    async def mystats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        report = self.monitor.reports.report_for(interaction.user.id)
        if report is None:
            await interaction.followup.send(
                say(TEXT.CMD_MYSTATS_NONE, vc=f"<#{self.config.voice_channel_id}>"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=self.monitor.reports.build_embed(report), ephemeral=True
        )

    # ----------------------------------------------------------- alive check

    @app_commands.command(
        name="alivecheck",
        description="Run an alive check right now (normally random every 1-6h).",
    )
    @is_host()
    @app_commands.guild_only()
    async def alivecheck(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not self.engine.is_running:
            await interaction.followup.send(
                say(TEXT.CMD_ALIVECHECK_NO_EVENT, status=self.engine.status.value), ephemeral=True
            )
            return
        if not self.config.alive_check_enabled:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_DISABLED, ephemeral=True)
            return
        if self.monitor.alive_checks.pending is not None:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_ALREADY, ephemeral=True)
            return
        started = await self.monitor.force_alive_check()
        if not started:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_FAILED, ephemeral=True)
            return
        await interaction.followup.send(
            say(
                TEXT.CMD_ALIVECHECK_STARTED,
                check_channel=f"<#{self.monitor.alive_io.channel_id()}>",
                minutes=int(self.config.alive_check_timeout_minutes),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------- message reloading

    @app_commands.command(
        name="reloadmessages",
        description="Re-read Announcements.py so wording changes apply without a restart.",
    )
    @is_host()
    @app_commands.guild_only()
    async def reloadmessages(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        ok, detail = reload_texts()
        if not ok:
            await interaction.followup.send(say(TEXT.CMD_MESSAGES_FAILED, error=detail[:1500]),
                                            ephemeral=True)
            return
        log.info("Announcements.py reloaded from %s by %s", texts_source(), interaction.user)
        await interaction.followup.send(
            say(TEXT.CMD_MESSAGES_RELOADED, count=message_count(), milestones=len(MILESTONES)),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ stop

    @app_commands.command(name="stop", description="Manually stop the event (CANCELLED, not FAILED).")
    @is_host()
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not self.engine.is_running:
            await interaction.response.send_message(
                say(TEXT.CMD_STOP_NOTHING, status=self.engine.status.value), ephemeral=True
            )
            return

        elapsed = self.engine.elapsed()
        view = ConfirmView(interaction.user.id, confirm_label="Stop the event")
        await interaction.response.send_message(
            say(TEXT.CMD_STOP_CONFIRM, elapsed=format_hm(elapsed), total=format_hm(TOTAL_SECONDS)),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            if view.value is None:
                await interaction.followup.send(TEXT.CMD_STOP_TIMEOUT, ephemeral=True)
            return

        try:
            async with self.monitor.lock:
                event = self.engine.cancel(by_user_id=interaction.user.id)
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await self.monitor.dispatch(event)
        await interaction.followup.send(TEXT.CMD_STOP_DONE, ephemeral=True)

    # ----------------------------------------------------------------- reset

    @app_commands.command(name="reset", description="Wipe all event data for a brand new run.")
    @is_host()
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ResetModal(self))

    # ------------------------------------------------------------ error path

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, NotAHost):
            message = str(error) or TEXT.CMD_NOT_A_HOST
        elif isinstance(error, app_commands.CheckFailure):
            message = say(
                TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"
            )
        else:
            log.exception("Command error", exc_info=error)
            message = TEXT.CMD_ERROR
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover
            pass
