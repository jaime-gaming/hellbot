"""Discord command handling — the `/hell` slash command group."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import RESTART_EXIT_CODE, __version__
from .errorcodes import lookup as _ec_lookup
from .config import Config
from .embeds import add_chunked_field
from .engine import HellEngine, StartError
from .health import preflight
from .milestones import MILESTONES, TOTAL_SECONDS
from .models import EventStatus
from .monitor import VoiceMonitor
from .tasks import active as active_tasks, spawn
from .texts import TEXT, message_count, say
from .texts import reload as reload_texts
from .texts import source as texts_source
from .timeutil import discord_ts, format_hm, now_ts
from .ui import CODE_LIFETIME_SECONDS, CodeGate, DMsClosed, NotAHost, NotOperator, dm_operator_only, is_host

__all__ = ["CodeGate", "HellCommands", "NotAHost", "is_host"]

log = logging.getLogger("hell.commands")


class HellCommands(commands.GroupCog, name="hell", description="Welcome to Hell event controls"):
    """`/hell start`, `/hell status`, `/hell leaderboard`, `/hell stop`, `/hell reset`."""

    def __init__(self, bot: commands.Bot, config: Config, engine: HellEngine, monitor: VoiceMonitor):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.monitor = monitor
        self.announcer = monitor.announcer
        self.code_gate = CodeGate()
        self._leaderboard_task: Optional[asyncio.Task] = None
        self._leaderboard_message: Optional[discord.Message] = None
        self._status_task: Optional[asyncio.Task] = None
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Log who runs what — an audit trail for every command in the group."""
        command = interaction.command.name if interaction.command else "?"
        log.info(
            "/hell %s by %s (%s)%s",
            command,
            interaction.user,
            interaction.user.id,
            f" [event {self.engine.status.value}]" if self.engine.event_uid else "",
        )
        return True

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
        # Hard requirement: an event never starts into an empty VC.  There is
        # no config toggle for this — a run nobody can win is not a run.
        if not humans:
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

    @app_commands.command(name="leaderboard", description="Show the Welcome to Hell leaderboard (auto-updates every minute).")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        entries = self.engine.leaderboard()
        frozen = self.engine.status.is_terminal
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LIVE LEADERBOARD"
        embeds = self.announcer.build_leaderboard_live_embeds(entries, title=title)
        if frozen and embeds:
            embeds[-1].set_footer(text="These rankings are frozen; the event is over.")
        msg = await interaction.followup.send(embeds=embeds)

        # Start auto-update if the event is running.
        if self.engine.is_running and not frozen:
            if self._leaderboard_task is not None and not self._leaderboard_task.done():
                self._leaderboard_task.cancel()
            self._leaderboard_message = msg
            self._leaderboard_task = spawn(
                self._leaderboard_update_loop(msg, title),
                name="leaderboard-update",
            )

    async def _leaderboard_update_loop(self, message: discord.Message, title: str) -> None:
        """Edit the leaderboard message every 60 seconds while the event runs."""
        try:
            while self.engine.is_running:
                await asyncio.sleep(60)
                try:
                    entries = self.engine.leaderboard()
                    embeds = self.announcer.build_leaderboard_live_embeds(entries, title=title)
                    await message.edit(embeds=embeds)
                except discord.NotFound:
                    break  # message deleted
                except discord.HTTPException:
                    pass  # retry next cycle

            # Event ended — write the final frozen state once.
            if not self.engine.is_running:
                try:
                    entries = self.engine.leaderboard()
                    final_title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD"
                    embeds = self.announcer.build_leaderboard_live_embeds(entries, title=final_title)
                    if embeds:
                        embeds[-1].set_footer(text="These rankings are frozen; the event is over.")
                    await message.edit(embeds=embeds)
                except (discord.NotFound, discord.HTTPException):
                    pass
        except asyncio.CancelledError:
            pass

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
                value=f"{self.announcer.embeds.reward(m)}\n{state}",
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
            app_commands.Choice(name="tail", value="tail"),
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
        elif choice == "tail":
            lines = stream.tail(20)
            message = (
                "```ansi\n" + "\n".join(lines)[-1900:] + "\n```"
                if lines
                else TEXT.CMD_LOGS_TAIL_EMPTY
            )
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
        if self.engine.is_paused:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_PAUSED, ephemeral=True)
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

    # ------------------------------------------------------------------ help

    @app_commands.command(name="help", description="What this event is and how to take part.")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        everyone: list[str] = []
        hosts: list[str] = []
        for command in sorted(self.app_command.commands, key=lambda c: c.name):  # type: ignore[union-attr]
            line = f"`/hell {command.name}` — {command.description}"
            restricted = bool(getattr(command, "checks", ()))
            (hosts if restricted else everyone).append(line)

        embed = discord.Embed(
            title=TEXT.CMD_HELP_TITLE,
            description=say(
                TEXT.CMD_HELP_DESCRIPTION,
                vc=f"<#{self.config.voice_channel_id}>",
                grace_seconds=int(self.config.empty_vc_grace_seconds),
            ),
            color=int(TEXT.COLOR_RUNNING),
        )
        add_chunked_field(embed, TEXT.CMD_HELP_EVERYONE_FIELD, "\n".join(everyone))
        add_chunked_field(
            embed,
            say(TEXT.CMD_HELP_HOST_FIELD, host_role=f"<@&{self.config.gamenight_host_role_id}>"),
            "\n".join(hosts),
        )
        add_chunked_field(
            embed,
            TEXT.CMD_HELP_RULES_FIELD,
            say(
                TEXT.CMD_HELP_RULES,
                clanker_role=f"<@&{self.config.clanker_role_id}>",
                alive_minutes=int(self.config.alive_check_timeout_minutes),
            ),
        )
        embed.set_footer(text=TEXT.CMD_HELP_FOOTER)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ user

    @app_commands.command(name="user", description="How long someone has spent in Hell.")
    @app_commands.describe(member="Whose time to show (defaults to you).")
    @app_commands.guild_only()
    async def user(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        target = member or interaction.user
        board = self.engine.leaderboard()
        entry = next((e for e in board if e.user_id == target.id), None)
        if entry is None:
            await interaction.followup.send(
                say(TEXT.CMD_USER_NO_TIME, who=target.mention), ephemeral=True
            )
            return

        elapsed = self.engine.elapsed()
        claimed = [
            record.hours
            for record in self.engine.milestone_records()
            if any(m.user_id == target.id for m in record.members)
        ]
        present = any(p.user_id == target.id for p in self.engine.last_participants)

        embed = discord.Embed(
            title=say(TEXT.CMD_USER_TITLE, name=target.display_name),
            description=TEXT.CMD_USER_PRESENT if present else TEXT.CMD_USER_ABSENT,
            color=int(TEXT.COLOR_RUNNING),
        )
        embed.add_field(
            name=TEXT.CMD_USER_TIME_FIELD, value=f"**{format_hm(entry.seconds)}**", inline=True
        )
        embed.add_field(
            name=TEXT.CMD_USER_RANK_FIELD,
            value=say(TEXT.CMD_USER_RANK_VALUE, rank=entry.rank, total=len(board)),
            inline=True,
        )
        if elapsed > 0:
            embed.add_field(
                name=TEXT.CMD_USER_SHARE_FIELD,
                value=f"**{min(100.0, entry.seconds / elapsed * 100):.0f}%**",
                inline=True,
            )
        add_chunked_field(
            embed,
            say(TEXT.CMD_USER_MILESTONES_FIELD, count=len(claimed)),
            ", ".join(f"**{hours}h**" for hours in claimed) or TEXT.CMD_USER_MILESTONES_NONE,
        )
        await interaction.followup.send(embed=embed)

    # ---------------------------------------------------------------- export

    @app_commands.command(
        name="export", description="Download the leaderboard as a CSV (for handing out rewards).")
    @is_host()
    @app_commands.guild_only()
    async def export(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        board = self.engine.leaderboard()
        if not board:
            await interaction.followup.send(TEXT.CMD_EXPORT_EMPTY, ephemeral=True)
            return

        claimed_by: dict[int, list[int]] = {}
        for record in self.engine.milestone_records():
            for participant in record.members:
                claimed_by.setdefault(participant.user_id, []).append(record.hours)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["rank", "user_id", "display_name", "seconds", "time", "milestones"])
        for entry in board:
            writer.writerow(
                [
                    entry.rank,
                    entry.user_id,
                    entry.display_name,
                    round(entry.seconds, 1),
                    format_hm(entry.seconds),
                    " ".join(f"{hours}h" for hours in sorted(claimed_by.get(entry.user_id, []))),
                ]
            )
        filename = f"welcome-to-hell-{self.engine.status.value.lower()}.csv"
        payload = discord.File(io.BytesIO(buffer.getvalue().encode("utf-8")), filename=filename)
        await interaction.followup.send(
            say(TEXT.CMD_EXPORT_DESCRIPTION, filename=filename, rows=len(board)),
            file=payload,
            ephemeral=True,
        )

    # -------------------------------------------------------------- restart

    @app_commands.command(
        name="restart",
        description="Restart the bot to apply updates. Only from DMs, operator only.",
    )
    @dm_operator_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        """Exit the process so the process manager restarts it with new code."""
        await interaction.response.send_message(TEXT.CMD_RESTART_DONE, ephemeral=True)
        log.warning(
            "Bot restart requested by %s (%s) — exiting with code %d",
            interaction.user, interaction.user.id, RESTART_EXIT_CODE,
        )
        stream = getattr(self.bot, "log_stream", None)
        if stream is not None:
            try:
                await stream.flush()
            except Exception:
                pass
        raise SystemExit(RESTART_EXIT_CODE)

    # ------------------------------------------------------------- security

    @app_commands.command(
        name="security",
        description="Anti-cheat and anomaly report for the operator.",
    )
    @is_host()
    @app_commands.guild_only()
    async def security(self, interaction: discord.Interaction) -> None:
        """Show security-relevant stats: flap detection, alive-check dodging,
        rate-limit bursts, and monitor health."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        snap = self.monitor.security.snapshot()
        lines: list[str] = []

        dodgers = snap.get("dodgers", [])
        if dodgers:
            lines.append(
                "🚨 **Alive-check dodgers**\n"
                + "\n".join(f"• <@{uid}> — {c} dodge(s)" for uid, c in dodgers)
            )
        else:
            lines.append("✅ **Alive-check dodging**: none detected")

        flappers = snap.get("flappers", [])
        if flappers:
            lines.append(
                "\n⚠️ **VC flapping**\n"
                + "\n".join(
                    f"• <@{uid}> — {c} join(s)/leave(s)" for uid, c in flappers[:5]
                )
            )
        else:
            lines.append("✅ **VC flapping**: none detected")

        rl = snap.get("rate_limits_5min", 0)
        lines.append(f"\n📊 **Rate limits (5 min)**: {rl}")

        stale = snap.get("stale_seconds")
        if stale is not None and stale > 60:
            lines.append(f"⚠️ **Monitor stale**: last VC observation {stale:.0f}s ago")
        else:
            lines.append("✅ **Monitor health**: ok")

        embed = discord.Embed(
            title="🛡️ Welcome to Hell — Security Report",
            description="\n".join(lines) or "No data collected yet.",
            color=int(TEXT.COLOR_IDLE),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --------------------------------------------------------------- errors

    @app_commands.command(
        name="errors",
        description="Look up an error code like HEL-100 for its full explanation.",
    )
    @app_commands.describe(code="The error code to look up (e.g. HEL-100).")
    @app_commands.guild_only()
    async def errors(self, interaction: discord.Interaction, code: str) -> None:
        """Show the full explanation of an error code."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        code = code.strip().upper()
        if not code.startswith("HEL-"):
            # Accept bare numbers too.
            code = f"HEL-{code}" if code.isdigit() else code
        ec = _ec_lookup(code)
        if ec is None:
            await interaction.followup.send(
                f"Unknown code: **{code}**. See the documentation for valid codes.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"Error code {ec.code}",
            description=ec.format(),
            color=int(TEXT.COLOR_RUNNING),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


    # ---------------------------------------------------------------- doctor

    @app_commands.command(
        name="doctor",
        description="Self-check: permissions, channels, roles, state and background tasks.",
    )
    @is_host()
    @app_commands.guild_only()
    async def doctor(self, interaction: discord.Interaction) -> None:
        """Everything an operator needs to diagnose the bot, in one place."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        report = await preflight(self.bot, self.config)
        ok = report.ok
        embed = discord.Embed(
            title=("🩺 All good" if ok else "🩺 Problems found"),
            description=(
                "Every check passed."
                if ok
                else "The bot is running, but these need attention:"
            ),
            color=int(TEXT.COLOR_RUNNING if ok else TEXT.COLOR_FAILED),
        )
        if report.errors:
            add_chunked_field(embed, "❌ Errors", "\n".join(f"• {e}" for e in report.errors))
        if report.warnings:
            add_chunked_field(embed, "⚠️ Warnings", "\n".join(f"• {w}" for w in report.warnings))

        state = self.engine.state
        vc_count = len(self.engine.last_participants)
        collected = await self.monitor.collect() if self.engine.is_running else None
        if collected is not None:
            vc_count = len(collected[0])
        paused = (
            f"**⏸️ paused** since {discord_ts(state.paused_ts, 'f')}"
            if state.paused_ts is not None
            else "no"
        )
        add_chunked_field(
            embed,
            "📊 State",
            "\n".join(
                [
                    f"• Status: **{state.status.value}**",
                    f"• Paused: {paused}",
                    f"• Elapsed: **{format_hm(self.engine.elapsed())}** / {format_hm(TOTAL_SECONDS)}",
                    f"• In the VC: **{vc_count}**",
                    f"• Milestones reached: **{len(self.engine.milestone_records())}**",
                    f"• Leaderboard rows: **{len(self.engine.leaderboard())}**",
                ]
            ),
        )
        alive = self.monitor.alive_checks.status_line(now_ts())
        blind = self.monitor.blind_seconds
        stream = getattr(self.bot, "log_stream", None)
        add_chunked_field(
            embed,
            "🔧 Runtime",
            "\n".join(
                [
                    f"• Version: **{__version__}**",
                    f"• Messages loaded from: `{texts_source()}`",
                    f"• Background tasks: **{active_tasks()}**",
                    f"• VC visibility: {f'**blind for {blind:.0f}s**' if blind else 'ok'}",
                    f"• Alive checks: {alive or 'disabled'}",
                    f"• Log stream: {stream.status() if stream else 'unavailable'}",
                ]
            ),
        )
        add_chunked_field(
            embed,
            "⚙️ Configuration",
            "\n".join(f"• {label}: `{value}`" for label, value in self.config.summary()),
        )
        embed.set_footer(text="Nothing here is a secret — the token is never shown.")
        await interaction.followup.send(embed=embed, ephemeral=True)

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

    # ------------------------------------------------------------- pause/run

    @app_commands.command(
        name="pause",
        description="Freeze everything (global + contestant timers) while a bug is fixed. Nothing can fail while paused.",
    )
    @is_host()
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        """Freeze the event so a problem can be fixed without punishing the run."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with self.monitor.lock:  # never race the 1s monitor tick
                self.engine.pause(
                    reason=f"requested by {interaction.user} ({interaction.user.id})"
                )
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        # A roll call in flight is cancelled, never enforced: nobody gets
        # kicked for failing to answer while the event is frozen.
        if self.monitor.alive_checks.pending is not None:
            await self.monitor.alive_checks.cancel(now_ts(), "the event was paused")
        log.warning("Event PAUSED by %s (%s)", interaction.user, interaction.user.id)
        await interaction.followup.send(TEXT.CMD_PAUSE_DONE, ephemeral=True)

    @app_commands.command(
        name="resume",
        description="Unfreeze the event after /hell pause — timers continue where they stopped.",
    )
    @is_host()
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        """Unfreeze the event; the paused time is never counted."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with self.monitor.lock:  # never race the 1s monitor tick
                self.engine.resume()
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if self.monitor.alive_checks.pending is not None:
            # A check left over from before the pause can never be answered
            # retroactively — clear it so fresh checks can be scheduled.
            await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
        log.warning("Event RESUMED by %s (%s)", interaction.user, interaction.user.id)
        await interaction.followup.send(TEXT.CMD_RESUME_DONE, ephemeral=True)

    # ------------------------------------------------------------------ stop

    @app_commands.command(
        name="stop",
        description="Stop the event (CANCELLED). Needs the approval code from the operator's DMs.",
    )
    @is_host()
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not self.engine.is_running:
            await interaction.response.send_message(
                say(TEXT.CMD_STOP_NOTHING, status=self.engine.status.value), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        if not await self._request_approval(interaction, "stop"):
            return
        await interaction.followup.send(TEXT.CMD_APPROVAL_REQUESTED, ephemeral=True)

    # ----------------------------------------------------------------- reset

    @app_commands.command(
        name="reset",
        description="Wipe all event data for a fresh run. Needs the approval code from the operator's DMs.",
    )
    @is_host()
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not await self._request_approval(interaction, "reset"):
            return
        await interaction.followup.send(TEXT.CMD_APPROVAL_REQUESTED, ephemeral=True)

    # --------------------------------------------------------------- approve

    @app_commands.command(
        name="approve",
        description="Confirm a dangerous action with the code sent to the operator's DMs.",
    )
    @app_commands.describe(code="The one-time code from the DM (6 characters).")
    @is_host()
    @app_commands.guild_only()
    async def approve(self, interaction: discord.Interaction, code: str) -> None:
        """Run the pending /hell stop or /hell reset once the operator's code is entered."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        expired = self.code_gate.expired_action
        if expired is not None:
            label = (
                TEXT.DANGER_ACTION_STOP if expired == "stop" else TEXT.DANGER_ACTION_RESET
            )
            await interaction.followup.send(
                say(TEXT.CMD_APPROVE_EXPIRED, action=label), ephemeral=True
            )
            return

        action = self.code_gate.pending_action
        if action is None:
            await interaction.followup.send(TEXT.CMD_APPROVE_NOTHING_PENDING, ephemeral=True)
            return

        # Stale-code guard: an approval was requested for a specific event.  If
        # that event ended and a NEW one is running, the code must not act on
        # the new event — request a fresh code instead.
        if action == "stop" and self.code_gate.pending_event_uid != self.engine.event_uid:
            self.code_gate.invalidate()
            await interaction.followup.send(TEXT.CMD_APPROVE_STALE_EVENT, ephemeral=True)
            return

        error = self.code_gate.redeem(action, code)
        if error is not None:
            await interaction.followup.send(
                say(TEXT.CMD_APPROVE_FAILED, error=error), ephemeral=True
            )
            return

        if action == "stop":
            try:
                async with self.monitor.lock:  # never race the 1s monitor tick
                    event = self.engine.cancel(by_user_id=interaction.user.id)
            except StartError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            await self.monitor.dispatch(event)
            await interaction.followup.send(TEXT.CMD_STOP_DONE, ephemeral=True)
        elif action == "reset":
            async with self.monitor.lock:
                was = self.engine.status
                self.engine.reset()
                self.monitor.alive_checks.reset()
            self.announcer.forget_progress_message()
            log.warning("Event data reset by %s (previous status: %s)", interaction.user, was.value)
            await interaction.followup.send(
                say(TEXT.CMD_RESET_DONE, previous_status=was.value), ephemeral=True
            )
        else:  # pragma: no cover - defensive
            await interaction.followup.send(TEXT.CMD_APPROVE_NOTHING_PENDING, ephemeral=True)

    async def _request_approval(self, interaction: discord.Interaction, action: str) -> bool:
        """Issue a one-time code and DM it to the operator (Jaime Gaming).

        Returns False — and replies to the interaction — when the code could
        not be delivered; the dangerous action must never proceed without the
        operator's code.  The code itself is never logged or echoed anywhere
        except the operator's DMs.
        """
        code = self.code_gate.issue(action, event_uid=self.engine.event_uid)
        label = TEXT.DANGER_ACTION_STOP if action == "stop" else TEXT.DANGER_ACTION_RESET
        requester = getattr(interaction.user, "display_name", None) or str(interaction.user)
        try:
            user = self.bot.get_user(self.config.log_dm_user_id)
            if user is None:
                user = await self.bot.fetch_user(self.config.log_dm_user_id)
            if user is None:
                self.code_gate.invalidate()
                await interaction.followup.send(
                    say(TEXT.CMD_APPROVAL_UNAVAILABLE, error="the operator could not be resolved"),
                    ephemeral=True,
                )
                return False
            await user.send(
                embed=discord.Embed(
                    title=TEXT.DANGER_CODE_TITLE,
                    description=say(
                        TEXT.DANGER_CODE_BODY,
                        action=label,
                        code=code,
                        expires=CODE_LIFETIME_SECONDS // 60,
                        requester=requester,
                    ),
                    color=int(TEXT.COLOR_FAILED),
                )
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.error("Could not deliver the approval code for %s: %s", action, exc)
            self.code_gate.invalidate()  # a code that never arrived must not be usable
            await interaction.followup.send(
                say(TEXT.CMD_APPROVAL_UNAVAILABLE, error=str(exc)[:400]), ephemeral=True
            )
            return False
        log.info("Approval code issued for %s by %s (%s)", action, interaction.user, interaction.user.id)
        return True

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
        # DM-only and operator-only specific messages
        if isinstance(error, DMsClosed):
            message = TEXT.CMD_DM_ONLY
        elif isinstance(error, NotOperator):
            message = TEXT.CMD_OPERATOR_ONLY
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover
            pass
