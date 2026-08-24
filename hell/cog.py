"""Discord command handling — the `/hell` slash commands and `!` prefix DM/chat commands."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import RESTART_EXIT_CODE, __version__
from .config import Config
from .embeds import add_chunked_field
from .engine import HellEngine, StartError
from .errorcodes import lookup as _ec_lookup
from .health import preflight
from .milestones import MILESTONES, TOTAL_SECONDS
from .models import EventStatus
from .monitor import VoiceMonitor
from .tasks import active as active_tasks
from .tasks import spawn
from .texts import TEXT, message_count, say
from .texts import reload as reload_texts
from .texts import source as texts_source
from .timeutil import discord_ts, format_hm, now_ts
from .ui import CODE_LIFETIME_SECONDS, CodeGate, DMsClosed, NotAHost, NotOperator, dm_operator_only, is_host

__all__ = ["CodeGate", "HellCommands", "NotAHost", "is_host"]

log = logging.getLogger("hell.commands")


class HellCommands(commands.GroupCog, name="hell", description="Welcome to Hell event controls"):
    """`/hell start`, `/hell status`, `/hell leaderboard`, `/hell stop`, `/hell reset`, etc.

    Also exposes `!` prefix commands in DMs and server text channels (e.g. `!status`, `!help`, `!hell status`).
    """

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

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Enforce that prefix commands can ONLY be run in Direct Messages (DMs)."""
        return ctx.guild is None

    # =========================================================================
    # Shared Helper Builders
    # =========================================================================

    def _build_status_embed(self, participants_count: Optional[int] = None) -> discord.Embed:
        if self.engine.status is EventStatus.IDLE:
            return discord.Embed(
                title=TEXT.CMD_IDLE_TITLE,
                description=say(
                    TEXT.CMD_IDLE_TEXT,
                    host_role=f"<@&{self.config.gamenight_host_role_id}>",
                    vc=f"<#{self.config.voice_channel_id}>",
                ),
                color=int(TEXT.COLOR_IDLE),
            )
        if participants_count is None:
            participants_count = len(self.engine.last_participants)
        snap = self.engine.snapshot(participants=participants_count)
        alive_line = self.monitor.alive_checks.status_line(now_ts()) if self.engine.is_running else None
        return self.announcer.build_status(snap, alive_line=alive_line)

    def _build_leaderboard_embeds(self) -> list[discord.Embed]:
        entries = self.engine.leaderboard()
        frozen = self.engine.status.is_terminal
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LIVE LEADERBOARD"
        embeds = self.announcer.build_leaderboard_live_embeds(entries, title=title)
        if frozen and embeds:
            embeds[-1].set_footer(text="These rankings are frozen; the event is over.")
        return embeds

    def _build_milestones_embed(self) -> discord.Embed:
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
        return embed

    def _build_mystats_payload(self, user_id: int) -> tuple[Optional[discord.Embed], Optional[str]]:
        report = self.monitor.reports.report_for(user_id)
        if report is None:
            return None, say(TEXT.CMD_MYSTATS_NONE, vc=f"<#{self.config.voice_channel_id}>")
        return self.monitor.reports.build_embed(report), None

    def _build_user_embed(self, user_id: int, display_name: str) -> tuple[Optional[discord.Embed], Optional[str]]:
        board = self.engine.leaderboard()
        entry = next((e for e in board if e.user_id == user_id), None)
        if entry is None:
            return None, say(TEXT.CMD_USER_NO_TIME, who=f"<@{user_id}>")

        elapsed = self.engine.elapsed()
        claimed = [
            record.hours
            for record in self.engine.milestone_records()
            if any(m.user_id == user_id for m in record.members)
        ]
        present = any(p.user_id == user_id for p in self.engine.last_participants)

        embed = discord.Embed(
            title=say(TEXT.CMD_USER_TITLE, name=display_name),
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
        return embed, None

    def _build_errors_payload(self, code: str) -> tuple[Optional[discord.Embed], Optional[str]]:
        code = code.strip().upper()
        if not code.startswith("HEL-"):
            code = f"HEL-{code}" if code.isdigit() else code
        ec = _ec_lookup(code)
        if ec is None:
            return None, f"Unknown code: **{code}**. See the documentation for valid codes."
        embed = discord.Embed(
            title=f"Error code {ec.code}",
            description=ec.format(),
            color=int(TEXT.COLOR_RUNNING),
        )
        return embed, None

    async def _build_doctor_embed(self) -> discord.Embed:
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
        return embed

    def _build_security_embed(self) -> discord.Embed:
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

        return discord.Embed(
            title="🛡️ Welcome to Hell — Security Report",
            description="\n".join(lines) or "No data collected yet.",
            color=int(TEXT.COLOR_IDLE),
        )

    def _build_export_file(self) -> tuple[Optional[discord.File], str]:
        board = self.engine.leaderboard()
        if not board:
            return None, TEXT.CMD_EXPORT_EMPTY

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
        return payload, say(TEXT.CMD_EXPORT_DESCRIPTION, filename=filename, rows=len(board))

    async def _handle_logs(self, choice: Optional[str] = None, lvl: Optional[str] = None, requester: Optional[Any] = None) -> str:
        stream = getattr(self.bot, "log_stream", None)
        if stream is None:
            return TEXT.CMD_LOGS_UNAVAILABLE

        choice = (choice or "status").lower()
        if lvl is not None:
            lvl_clean = lvl.strip().upper()
            stream.set_level(lvl_clean)
            log.info("Live log level set to %s by %s", lvl_clean, requester or "unknown")

        if choice == "on":
            stream.set_enabled(True)
            if not stream.running:
                await stream.start()
            return TEXT.CMD_LOGS_ON
        elif choice == "off":
            stream.set_enabled(False)
            return TEXT.CMD_LOGS_OFF
        elif choice == "test":
            log.warning("Live log test triggered by %s", requester or "unknown")
            await stream.flush()
            return TEXT.CMD_LOGS_TEST
        elif choice == "tail":
            lines = stream.tail(20)
            return (
                "```ansi\n" + "\n".join(lines)[-1900:] + "\n```"
                if lines
                else TEXT.CMD_LOGS_TAIL_EMPTY
            )
        elif choice == "flush":
            sent = await stream.flush()
            return say(TEXT.CMD_LOGS_FLUSHED, sent=sent)
        else:
            return say(TEXT.CMD_LOGS_STATUS, status=stream.status())

    async def _is_host_or_operator(self, ctx: commands.Context) -> bool:
        if ctx.author.id == self.config.log_dm_user_id:
            return True
        if isinstance(ctx.author, discord.Member):
            return any(r.id == self.config.gamenight_host_role_id for r in ctx.author.roles)
        guild = ctx.bot.get_guild(self.config.guild_id)
        if guild is not None:
            member = guild.get_member(ctx.author.id)
            if member is None:
                try:
                    member = await guild.fetch_member(ctx.author.id)
                except Exception:
                    member = None
            if member is not None:
                return any(r.id == self.config.gamenight_host_role_id for r in member.roles)
        roles = getattr(ctx.author, "roles", None)
        if roles:
            return any(getattr(r, "id", None) == self.config.gamenight_host_role_id for r in roles)
        return False

    def _is_operator(self, ctx: commands.Context) -> bool:
        return ctx.author.id == self.config.log_dm_user_id

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
        if not humans:
            await interaction.followup.send(
                say(TEXT.CMD_VC_EMPTY_ON_START, vc=f"<#{self.config.voice_channel_id}>"),
                ephemeral=True,
            )
            return

        assert interaction.guild is not None
        try:
            async with self.monitor.lock:
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

        self.monitor.alive_checks.bind(self.engine.event_uid, now=now_ts())

        self.announcer.forget_progress_message()
        snap = self.engine.snapshot(participants=len(humans))
        await self.announcer.announce_start(snap, interaction.user, humans)
        await self.announcer.update_progress(snap)
        self.monitor.sync_status()
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
        count = len(self.engine.last_participants)
        if self.engine.is_running:
            collected = await self.monitor.collect()
            if collected is not None:
                count = len(collected[0])
        embed = self._build_status_embed(count)
        await interaction.followup.send(embed=embed)

    # ----------------------------------------------------------- leaderboard

    @app_commands.command(name="leaderboard", description="Show the Welcome to Hell leaderboard (auto-updates every minute).")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        embeds = self._build_leaderboard_embeds()
        msg = await interaction.followup.send(embeds=embeds, wait=True)

        frozen = self.engine.status.is_terminal
        if self.engine.is_running and not frozen:
            if self._leaderboard_task is not None and not self._leaderboard_task.done():
                self._leaderboard_task.cancel()
            self._leaderboard_message = msg
            title = "🏆 WELCOME TO HELL — LIVE LEADERBOARD"
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
                    break
                except discord.HTTPException:
                    pass

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
        embed = self._build_milestones_embed()
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
        choice = action.value if action else "status"
        lvl = level.value if level else None
        message = await self._handle_logs(choice, lvl, requester=interaction.user)
        await interaction.followup.send(message, ephemeral=True)

    # --------------------------------------------------------------- my stats

    @app_commands.command(name="mystats", description="Your personal Welcome to Hell stat card.")
    @app_commands.guild_only()
    async def mystats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed, text = self._build_mystats_payload(interaction.user.id)
        if embed is None:
            await interaction.followup.send(
                text or say(TEXT.CMD_MYSTATS_NONE, vc=f"<#{self.config.voice_channel_id}>"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

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
        embed, err = self._build_user_embed(target.id, target.display_name)
        if embed is None:
            await interaction.followup.send(
                err or say(TEXT.CMD_USER_NO_TIME, who=target.mention), ephemeral=True
            )
            return
        await interaction.followup.send(embed=embed)

    # ---------------------------------------------------------------- export

    @app_commands.command(
        name="export", description="Download the leaderboard as a CSV (for handing out rewards).")
    @is_host()
    @app_commands.guild_only()
    async def export(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        payload, text = self._build_export_file()
        if payload is None:
            await interaction.followup.send(text or TEXT.CMD_EXPORT_EMPTY, ephemeral=True)
            return
        await interaction.followup.send(
            text,
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
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = self._build_security_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --------------------------------------------------------------- errors

    @app_commands.command(
        name="errors",
        description="Look up an error code like HEL-100 for its full explanation.",
    )
    @app_commands.describe(code="The error code to look up (e.g. HEL-100).")
    @app_commands.guild_only()
    async def errors(self, interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed, err = self._build_errors_payload(code)
        if embed is None:
            await interaction.followup.send(err or f"Unknown code: **{code}**.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------- doctor

    @app_commands.command(
        name="doctor",
        description="Self-check: permissions, channels, roles, state and background tasks.",
    )
    @is_host()
    @app_commands.guild_only()
    async def doctor(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        embed = await self._build_doctor_embed()
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
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with self.monitor.lock:
                self.engine.pause(
                    reason=f"requested by {interaction.user} ({interaction.user.id})"
                )
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if self.monitor.alive_checks.pending is not None:
            await self.monitor.alive_checks.cancel(now_ts(), "the event was paused")
        log.warning("Event PAUSED by %s (%s)", interaction.user, interaction.user.id)
        self.monitor.sync_status()
        await interaction.followup.send(TEXT.CMD_PAUSE_DONE, ephemeral=True)

    @app_commands.command(
        name="resume",
        description="Unfreeze the event after /hell pause or continue a failed run (needs MFA).",
    )
    @is_host()
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if self.engine.status is EventStatus.FAILED:
            await interaction.response.defer(thinking=True, ephemeral=True)
            if not await self._request_approval(interaction, "resume"):
                return
            await interaction.followup.send(TEXT.CMD_APPROVAL_REQUESTED, ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with self.monitor.lock:
                self.engine.resume()
        except StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if self.monitor.alive_checks.pending is not None:
            await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
        log.warning("Event RESUMED by %s (%s)", interaction.user, interaction.user.id)
        self.monitor.sync_status()
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
        await interaction.response.defer(thinking=True, ephemeral=True)

        expired = self.code_gate.expired_action
        if expired is not None:
            label = (
                TEXT.DANGER_ACTION_STOP
                if expired == "stop"
                else (
                    TEXT.DANGER_ACTION_RESET
                    if expired == "reset"
                    else getattr(TEXT, "DANGER_ACTION_RESUME", "Resuming the failed event")
                )
            )
            await interaction.followup.send(
                say(TEXT.CMD_APPROVE_EXPIRED, action=label), ephemeral=True
            )
            return

        action = self.code_gate.pending_action
        if action is None:
            await interaction.followup.send(TEXT.CMD_APPROVE_NOTHING_PENDING, ephemeral=True)
            return

        if action in ("stop", "resume") and self.code_gate.pending_event_uid != self.engine.event_uid:
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
                async with self.monitor.lock:
                    event = self.engine.cancel(by_user_id=interaction.user.id)
            except StartError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            await self.monitor.dispatch(event)
            self.monitor.sync_status()
            await interaction.followup.send(TEXT.CMD_STOP_DONE, ephemeral=True)
        elif action == "reset":
            async with self.monitor.lock:
                was = self.engine.status
                self.engine.reset()
                self.monitor.alive_checks.reset()
            self.announcer.forget_progress_message()
            log.warning("Event data reset by %s (previous status: %s)", interaction.user, was.value)
            self.monitor.sync_status()
            await interaction.followup.send(
                say(TEXT.CMD_RESET_DONE, previous_status=was.value), ephemeral=True
            )
        elif action == "resume":
            try:
                async with self.monitor.lock:
                    self.engine.resume_failed()
            except StartError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            if self.monitor.alive_checks.pending is not None:
                await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
            self.announcer.forget_progress_message()
            count = len(self.engine.last_participants)
            snap = self.engine.snapshot(participants=count)
            await self.announcer.update_progress(snap, force=True)
            log.warning("Failed event RESUMED by %s (%s)", interaction.user, interaction.user.id)
            self.monitor.sync_status()
            await interaction.followup.send(TEXT.CMD_RESUME_DONE, ephemeral=True)
        else:
            await interaction.followup.send(TEXT.CMD_APPROVE_NOTHING_PENDING, ephemeral=True)

    async def _request_approval(self, interaction: discord.Interaction, action: str) -> bool:
        code = self.code_gate.issue(action, event_uid=self.engine.event_uid)
        label = (
            TEXT.DANGER_ACTION_STOP
            if action == "stop"
            else (
                TEXT.DANGER_ACTION_RESET
                if action == "reset"
                else getattr(TEXT, "DANGER_ACTION_RESUME", "Resuming the failed event")
            )
        )
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
            self.code_gate.invalidate()
            await interaction.followup.send(
                say(TEXT.CMD_APPROVAL_UNAVAILABLE, error=str(exc)[:400]), ephemeral=True
            )
            return False
        log.info("Approval code issued for %s by %s (%s)", action, interaction.user, interaction.user.id)
        return True

    async def _request_approval_dm(self, ctx: commands.Context, action: str) -> bool:
        code = self.code_gate.issue(action, event_uid=self.engine.event_uid)
        label = (
            TEXT.DANGER_ACTION_STOP
            if action == "stop"
            else (
                TEXT.DANGER_ACTION_RESET
                if action == "reset"
                else getattr(TEXT, "DANGER_ACTION_RESUME", "Resuming the failed event")
            )
        )
        requester = getattr(ctx.author, "display_name", None) or str(ctx.author)
        try:
            user = self.bot.get_user(self.config.log_dm_user_id)
            if user is None:
                user = await self.bot.fetch_user(self.config.log_dm_user_id)
            if user is None:
                self.code_gate.invalidate()
                await ctx.send(
                    say(TEXT.CMD_APPROVAL_UNAVAILABLE, error="the operator could not be resolved")
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
            self.code_gate.invalidate()
            await ctx.send(
                say(TEXT.CMD_APPROVAL_UNAVAILABLE, error=str(exc)[:400])
            )
            return False
        log.info("Approval code issued for %s by %s (%s) via prefix command", action, ctx.author, ctx.author.id)
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
        if isinstance(error, DMsClosed):
            message = TEXT.CMD_DM_ONLY
        elif isinstance(error, NotOperator):
            message = TEXT.CMD_OPERATOR_ONLY
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    # =========================================================================
    # Prefix Commands (for DMs and chat channels: !status, !help, !hell ...)
    # =========================================================================

    async def _exec_status(self, ctx: commands.Context) -> None:
        count = len(self.engine.last_participants)
        if self.engine.is_running:
            collected = await self.monitor.collect()
            if collected is not None:
                count = len(collected[0])
        embed = self._build_status_embed(count)
        await ctx.send(embed=embed)

    async def _exec_leaderboard(self, ctx: commands.Context) -> None:
        embeds = self._build_leaderboard_embeds()
        await ctx.send(embeds=embeds)

    async def _exec_milestones(self, ctx: commands.Context) -> None:
        embed = self._build_milestones_embed()
        await ctx.send(embed=embed)

    async def _exec_mystats(self, ctx: commands.Context) -> None:
        embed, text = self._build_mystats_payload(ctx.author.id)
        if embed is not None:
            await ctx.send(embed=embed)
        else:
            await ctx.send(text or say(TEXT.CMD_MYSTATS_NONE, vc=f"<#{self.config.voice_channel_id}>"))

    async def _exec_user(self, ctx: commands.Context, member: Optional[str] = None) -> None:
        target_id: int
        display_name: str
        if not member:
            target_id = ctx.author.id
            display_name = getattr(ctx.author, "display_name", None) or str(ctx.author)
        else:
            clean = member.strip()
            m = re.match(r"^<@!?(\d+)>$", clean)
            if m:
                target_id = int(m.group(1))
                display_name = clean
            elif clean.isdigit():
                target_id = int(clean)
                display_name = f"<@{target_id}>"
            else:
                board = self.engine.leaderboard()
                entry = next((e for e in board if e.display_name.lower() == clean.lower()), None)
                if entry is None:
                    entry = next((e for e in board if clean.lower() in e.display_name.lower()), None)
                if entry is not None:
                    target_id = entry.user_id
                    display_name = entry.display_name
                else:
                    await ctx.send(say(TEXT.CMD_USER_NO_TIME, who=clean))
                    return

        embed, err_text = self._build_user_embed(target_id, display_name)
        if embed is not None:
            await ctx.send(embed=embed)
        else:
            await ctx.send(err_text or say(TEXT.CMD_USER_NO_TIME, who=display_name))

    async def _exec_errors(self, ctx: commands.Context, code: Optional[str] = None) -> None:
        if not code:
            await ctx.send("Please specify an error code to look up (e.g. `!errors HEL-100` or `!errors 100`).")
            return
        embed, err_text = self._build_errors_payload(code)
        if embed is not None:
            await ctx.send(embed=embed)
        else:
            await ctx.send(err_text or f"Unknown code: **{code}**.")

    async def _exec_help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="🔥 Welcome to Hell — Commands",
            description=(
                "Use `!<command>` in DMs or `/hell <command>` in the server.\n"
                f"**Voice channel:** <#{self.config.voice_channel_id}>\n"
                f"**Empty-VC grace period:** {int(self.config.empty_vc_grace_seconds)}s\n"
            ),
            color=int(TEXT.COLOR_RUNNING),
        )
        add_chunked_field(
            embed,
            "👥 Public Commands (Everyone)",
            "\n".join([
                "`!status` (or `!st`) — Show current event status, elapsed time & VC count",
                "`!leaderboard` (or `!lb`, `!top`) — Show current or final rankings",
                "`!mystats` (or `!stats`, `!me`) — Your personal stat card",
                "`!user [@user/id/name]` — Look up anyone's time, rank & milestones",
                "`!milestones` (or `!ms`) — Milestones, rewards & list of claimants",
                "`!errors <code>` — Look up an error code explanation (e.g. `!errors HEL-100`)",
                "`!help` — Show this command help list",
            ]),
        )
        add_chunked_field(
            embed,
            say("👑 Host & Operator Commands ({host_role} / Operator)", host_role=f"<@&{self.config.gamenight_host_role_id}>"),
            "\n".join([
                "`!doctor` — Diagnostic self-check (permissions, state, runtime, config)",
                "`!logs [status|on|off|test|tail|flush] [level]` — Control live log stream",
                "`!security` — Anti-cheat and anomaly report",
                "`!export` — Download leaderboard CSV file",
                "`!alivecheck` — Force an immediate roll call",
                "`!reloadmessages` — Hot reload Announcements.py",
                "`!pause` / `!resume` — Freeze / unfreeze the event",
                "`!stop` / `!reset` — Request approval code to cancel or wipe run",
                "`!approve <code>` — Enter approval code to execute pending action",
                "`!start` — Start the 160h challenge if VC has valid humans",
                "`!restart` — (Operator only in DMs) Restart the bot process",
            ]),
        )
        add_chunked_field(
            embed,
            "📜 Event Rules",
            say(
                TEXT.CMD_HELP_RULES,
                clanker_role=f"<@&{self.config.clanker_role_id}>",
                alive_minutes=int(self.config.alive_check_timeout_minutes),
            ),
        )
        embed.set_footer(text=TEXT.CMD_HELP_FOOTER)
        await ctx.send(embed=embed)

    async def _exec_restart(self, ctx: commands.Context) -> None:
        if not self._is_operator(ctx):
            await ctx.send(TEXT.CMD_OPERATOR_ONLY)
            return
        await ctx.send(TEXT.CMD_RESTART_DONE)
        log.warning(
            "Bot restart requested via DM prefix command by %s (%s) — exiting with code %d",
            ctx.author, ctx.author.id, RESTART_EXIT_CODE,
        )
        stream = getattr(self.bot, "log_stream", None)
        if stream is not None:
            try:
                await stream.flush()
            except Exception:
                pass
        raise SystemExit(RESTART_EXIT_CODE)

    async def _exec_doctor(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        embed = await self._build_doctor_embed()
        await ctx.send(embed=embed)

    async def _exec_logs(self, ctx: commands.Context, action: Optional[str] = None, level: Optional[str] = None) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        message = await self._handle_logs(action, level, requester=ctx.author)
        await ctx.send(message)

    async def _exec_security(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        embed = self._build_security_embed()
        await ctx.send(embed=embed)

    async def _exec_export(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        payload, text = self._build_export_file()
        if payload is None:
            await ctx.send(text or TEXT.CMD_EXPORT_EMPTY)
        else:
            await ctx.send(text, file=payload)

    async def _exec_reloadmessages(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        ok, detail = reload_texts()
        if not ok:
            await ctx.send(say(TEXT.CMD_MESSAGES_FAILED, error=detail[:1500]))
            return
        log.info("Announcements.py reloaded from %s by %s via prefix command", texts_source(), ctx.author)
        await ctx.send(say(TEXT.CMD_MESSAGES_RELOADED, count=message_count(), milestones=len(MILESTONES)))

    async def _exec_alivecheck(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not self.engine.is_running:
            await ctx.send(say(TEXT.CMD_ALIVECHECK_NO_EVENT, status=self.engine.status.value))
            return
        if not self.config.alive_check_enabled:
            await ctx.send(TEXT.CMD_ALIVECHECK_DISABLED)
            return
        if self.engine.is_paused:
            await ctx.send(TEXT.CMD_ALIVECHECK_PAUSED)
            return
        if self.monitor.alive_checks.pending is not None:
            await ctx.send(TEXT.CMD_ALIVECHECK_ALREADY)
            return
        started = await self.monitor.force_alive_check()
        if not started:
            await ctx.send(TEXT.CMD_ALIVECHECK_FAILED)
            return
        await ctx.send(
            say(
                TEXT.CMD_ALIVECHECK_STARTED,
                check_channel=f"<#{self.monitor.alive_io.channel_id()}>",
                minutes=int(self.config.alive_check_timeout_minutes),
            )
        )

    async def _exec_pause(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not self.engine.is_running:
            await ctx.send(say(TEXT.CMD_STOP_NOTHING, status=self.engine.status.value))
            return
        try:
            async with self.monitor.lock:
                self.engine.pause(reason=f"requested by {ctx.author} ({ctx.author.id})")
        except StartError as exc:
            await ctx.send(f"❌ {exc}")
            return
        if self.monitor.alive_checks.pending is not None:
            await self.monitor.alive_checks.cancel(now_ts(), "the event was paused")
        log.warning("Event PAUSED by %s (%s) via prefix command", ctx.author, ctx.author.id)
        self.monitor.sync_status()
        await ctx.send(TEXT.CMD_PAUSE_DONE)

    async def _exec_resume(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if self.engine.status is EventStatus.FAILED:
            if not await self._request_approval_dm(ctx, "resume"):
                return
            await ctx.send(TEXT.CMD_APPROVAL_REQUESTED)
            return
        if not self.engine.is_paused:
            await ctx.send("Event is not paused or failed.")
            return
        try:
            async with self.monitor.lock:
                self.engine.resume()
        except StartError as exc:
            await ctx.send(f"❌ {exc}")
            return
        if self.monitor.alive_checks.pending is not None:
            await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
        log.warning("Event RESUMED by %s (%s) via prefix command", ctx.author, ctx.author.id)
        self.monitor.sync_status()
        await ctx.send(TEXT.CMD_RESUME_DONE)

    async def _exec_stop(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not self.engine.is_running:
            await ctx.send(say(TEXT.CMD_STOP_NOTHING, status=self.engine.status.value))
            return
        if not await self._request_approval_dm(ctx, "stop"):
            return
        await ctx.send(TEXT.CMD_APPROVAL_REQUESTED)

    async def _exec_reset(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not await self._request_approval_dm(ctx, "reset"):
            return
        await ctx.send(TEXT.CMD_APPROVAL_REQUESTED)

    async def _exec_approve(self, ctx: commands.Context, code: Optional[str] = None) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not code:
            await ctx.send("Please provide the 6-character code (e.g. `!approve ABC123`).")
            return

        expired = self.code_gate.expired_action
        if expired is not None:
            label = (
                TEXT.DANGER_ACTION_STOP
                if expired == "stop"
                else (
                    TEXT.DANGER_ACTION_RESET
                    if expired == "reset"
                    else getattr(TEXT, "DANGER_ACTION_RESUME", "Resuming the failed event")
                )
            )
            await ctx.send(say(TEXT.CMD_APPROVE_EXPIRED, action=label))
            return

        action = self.code_gate.pending_action
        if action is None:
            await ctx.send(TEXT.CMD_APPROVE_NOTHING_PENDING)
            return

        if action in ("stop", "resume") and self.code_gate.pending_event_uid != self.engine.event_uid:
            self.code_gate.invalidate()
            await ctx.send(TEXT.CMD_APPROVE_STALE_EVENT)
            return

        error = self.code_gate.redeem(action, code)
        if error is not None:
            await ctx.send(say(TEXT.CMD_APPROVE_FAILED, error=error))
            return

        if action == "stop":
            try:
                async with self.monitor.lock:
                    event = self.engine.cancel(by_user_id=ctx.author.id)
            except StartError as exc:
                await ctx.send(f"❌ {exc}")
                return
            await self.monitor.dispatch(event)
            self.monitor.sync_status()
            await ctx.send(TEXT.CMD_STOP_DONE)
        elif action == "reset":
            async with self.monitor.lock:
                was = self.engine.status
                self.engine.reset()
                self.monitor.alive_checks.reset()
            self.announcer.forget_progress_message()
            log.warning("Event data reset by %s (previous status: %s)", ctx.author, was.value)
            self.monitor.sync_status()
            await ctx.send(say(TEXT.CMD_RESET_DONE, previous_status=was.value))
        elif action == "resume":
            try:
                async with self.monitor.lock:
                    self.engine.resume_failed()
            except StartError as exc:
                await ctx.send(f"❌ {exc}")
                return
            if self.monitor.alive_checks.pending is not None:
                await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
            self.announcer.forget_progress_message()
            count = len(self.engine.last_participants)
            snap = self.engine.snapshot(participants=count)
            await self.announcer.update_progress(snap, force=True)
            log.warning("Failed event RESUMED by %s (%s)", ctx.author, ctx.author.id)
            self.monitor.sync_status()
            await ctx.send(TEXT.CMD_RESUME_DONE)

    async def _exec_start(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if self.engine.is_running:
            await ctx.send(say(TEXT.CMD_ALREADY_RUNNING, elapsed=format_hm(self.engine.elapsed())))
            return
        if self.engine.status.is_terminal and not self.engine.state.final_saved:
            self.engine.freeze_leaderboard()

        collected = await self.monitor.collect()
        if collected is None:
            await ctx.send(say(TEXT.CMD_VC_UNREACHABLE, vc=f"<#{self.config.voice_channel_id}>"))
            return
        humans, clankers = collected
        if clankers:
            await self.monitor.kick_clankers(clankers)
        if not humans:
            await ctx.send(say(TEXT.CMD_VC_EMPTY_ON_START, vc=f"<#{self.config.voice_channel_id}>"))
            return

        guild_id = self.config.guild_id
        try:
            async with self.monitor.lock:
                if self.engine.is_running:
                    raise StartError("An event is already RUNNING.")
                self.engine.start(
                    now=now_ts(),
                    guild_id=guild_id,
                    voice_channel_id=self.config.voice_channel_id,
                    announce_channel_id=self.config.announce_channel_id,
                    started_by=ctx.author.id,
                    initial_participants=humans,
                )
        except StartError as exc:
            await ctx.send(f"❌ {exc}")
            return

        self.monitor.alive_checks.bind(self.engine.event_uid, now=now_ts())
        self.announcer.forget_progress_message()
        snap = self.engine.snapshot(participants=len(humans))
        await self.announcer.announce_start(snap, ctx.author, humans)
        await self.announcer.update_progress(snap)
        self.monitor.sync_status()
        await ctx.send(
            say(
                TEXT.CMD_STARTED,
                started_at=discord_ts(snap.start_ts or 0, "T"),
                total=format_hm(TOTAL_SECONDS),
                vc=f"<#{self.config.voice_channel_id}>",
                announce_channel=f"<#{self.config.announce_channel_id}>",
            )
        )

    # ------------------------------------------------------------- command hooks

    @commands.group(name="hell", invoke_without_command=True)
    async def prefix_hell_group(self, ctx: commands.Context, subcommand: Optional[str] = None, *, rest: Optional[str] = None) -> None:
        """Root command group: !hell <subcommand>."""
        if not subcommand:
            await self._exec_status(ctx)
            return
        sub = subcommand.lower()
        if sub in ("status", "st"):
            await self._exec_status(ctx)
        elif sub in ("leaderboard", "lb", "top"):
            await self._exec_leaderboard(ctx)
        elif sub in ("milestones", "ms"):
            await self._exec_milestones(ctx)
        elif sub in ("mystats", "stats", "me"):
            await self._exec_mystats(ctx)
        elif sub in ("user", "whois", "profile"):
            await self._exec_user(ctx, member=rest)
        elif sub in ("errors", "error", "err"):
            await self._exec_errors(ctx, code=rest)
        elif sub in ("help", "h", "commands"):
            await self._exec_help(ctx)
        elif sub == "restart":
            await self._exec_restart(ctx)
        elif sub in ("doctor", "diag", "health"):
            await self._exec_doctor(ctx)
        elif sub in ("logs", "log"):
            parts = rest.split() if rest else []
            act = parts[0] if parts else None
            lvl = parts[1] if len(parts) > 1 else None
            await self._exec_logs(ctx, action=act, level=lvl)
        elif sub in ("security", "sec"):
            await self._exec_security(ctx)
        elif sub == "export":
            await self._exec_export(ctx)
        elif sub in ("reloadmessages", "reload"):
            await self._exec_reloadmessages(ctx)
        elif sub == "alivecheck":
            await self._exec_alivecheck(ctx)
        elif sub == "pause":
            await self._exec_pause(ctx)
        elif sub == "resume":
            await self._exec_resume(ctx)
        elif sub == "stop":
            await self._exec_stop(ctx)
        elif sub == "reset":
            await self._exec_reset(ctx)
        elif sub == "approve":
            await self._exec_approve(ctx, code=rest)
        elif sub == "start":
            await self._exec_start(ctx)
        else:
            await ctx.send(f"Unknown subcommand `{subcommand}`. Type `!help` or `!hell help` for available commands.")

    @commands.command(name="status", aliases=["st"])
    async def prefix_status(self, ctx: commands.Context) -> None:
        """Show the current Welcome to Hell status."""
        await self._exec_status(ctx)

    @commands.command(name="leaderboard", aliases=["lb", "top"])
    async def prefix_leaderboard(self, ctx: commands.Context) -> None:
        """Show the Welcome to Hell leaderboard."""
        await self._exec_leaderboard(ctx)

    @commands.command(name="milestones", aliases=["ms"])
    async def prefix_milestones(self, ctx: commands.Context) -> None:
        """Show every milestone, its reward and who claimed it."""
        await self._exec_milestones(ctx)

    @commands.command(name="mystats", aliases=["stats", "me"])
    async def prefix_mystats(self, ctx: commands.Context) -> None:
        """Your personal Welcome to Hell stat card."""
        await self._exec_mystats(ctx)

    @commands.command(name="user", aliases=["whois", "profile"])
    async def prefix_user(self, ctx: commands.Context, *, member: Optional[str] = None) -> None:
        """How long someone has spent in Hell."""
        await self._exec_user(ctx, member=member)

    @commands.command(name="errors", aliases=["error", "err"])
    async def prefix_errors(self, ctx: commands.Context, *, code: Optional[str] = None) -> None:
        """Look up an error code like HEL-100."""
        await self._exec_errors(ctx, code=code)

    @commands.command(name="help", aliases=["h", "commands"])
    async def prefix_help(self, ctx: commands.Context) -> None:
        """What this event is and how to take part."""
        await self._exec_help(ctx)

    @commands.command(name="restart")
    async def prefix_restart(self, ctx: commands.Context) -> None:
        """Restart the bot to apply updates (operator only in DMs)."""
        await self._exec_restart(ctx)

    @commands.command(name="doctor", aliases=["diag", "health"])
    async def prefix_doctor(self, ctx: commands.Context) -> None:
        """Diagnostic self-check."""
        await self._exec_doctor(ctx)

    @commands.command(name="logs", aliases=["log"])
    async def prefix_logs(self, ctx: commands.Context, action: Optional[str] = None, level: Optional[str] = None) -> None:
        """Control the live log stream."""
        await self._exec_logs(ctx, action=action, level=level)

    @commands.command(name="security", aliases=["sec"])
    async def prefix_security(self, ctx: commands.Context) -> None:
        """Anti-cheat and anomaly report."""
        await self._exec_security(ctx)

    @commands.command(name="export")
    async def prefix_export(self, ctx: commands.Context) -> None:
        """Download the leaderboard as CSV."""
        await self._exec_export(ctx)

    @commands.command(name="reloadmessages", aliases=["reload"])
    async def prefix_reloadmessages(self, ctx: commands.Context) -> None:
        """Re-read Announcements.py."""
        await self._exec_reloadmessages(ctx)

    @commands.command(name="alivecheck")
    async def prefix_alivecheck(self, ctx: commands.Context) -> None:
        """Trigger an immediate alive check in the server."""
        await self._exec_alivecheck(ctx)

    @commands.command(name="pause")
    async def prefix_pause(self, ctx: commands.Context) -> None:
        """Freeze the event."""
        await self._exec_pause(ctx)

    @commands.command(name="resume")
    async def prefix_resume(self, ctx: commands.Context) -> None:
        """Unfreeze the event; or continue a failed run."""
        await self._exec_resume(ctx)

    @commands.command(name="stop")
    async def prefix_stop(self, ctx: commands.Context) -> None:
        """Stop the event (CANCELLED)."""
        await self._exec_stop(ctx)

    @commands.command(name="reset")
    async def prefix_reset(self, ctx: commands.Context) -> None:
        """Wipe all event data."""
        await self._exec_reset(ctx)

    @commands.command(name="approve")
    async def prefix_approve(self, ctx: commands.Context, code: Optional[str] = None) -> None:
        """Confirm a dangerous action with approval code."""
        await self._exec_approve(ctx, code=code)

    @commands.command(name="start")
    async def prefix_start(self, ctx: commands.Context) -> None:
        """Start Welcome to Hell (160h)."""
        await self._exec_start(ctx)
