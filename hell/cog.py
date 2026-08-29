"""Discord command handling — the `/hell` slash commands and `!` prefix DM/chat commands."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import random
import re
import time
from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from . import RESTART_EXIT_CODE, __version__
from .broadcast import BroadcastResult, dm_participants
from .config import Config
from .embeds import MAX_DESCRIPTION, add_chunked_field
from .engine import HellEngine, StartError
from .errorcodes import lookup as _ec_lookup
from .gamble import (
    MIN_BET_HOURS,
    GambleBook,
    GambleStats,
    effective_cooldown,
    format_mute,
    format_wait,
    gamble_time_loss_multiplier,
    parse_bet_hours,
    resolve_gamble,
    snap_bet_hours,
)
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
from .ui import (
    CODE_LIFETIME_SECONDS,
    CodeGate,
    DMsClosed,
    NotAHost,
    NotOperator,
    dm_host_only,
    dm_operator_only,
    is_host,
)

__all__ = ["CodeGate", "HellCommands", "NotAHost", "is_host"]

log = logging.getLogger("hell.commands")

_BROADCAST_LEVELS: dict[str, tuple[str, str, int]] = {
    "info": ("ℹ️", "INFO", None),
    "success": ("✅", "SUCCESS", None),
    "warning": ("⚠️", "WARNING", None),
    "error": ("🚨", "ERROR", None),
    "debug": ("🐞", "DEBUG", None),
    "milestone": ("🔥", "MILESTONE", None),
    "idle": ("💤", "IDLE", None),
    "grace": ("⏳", "GRACE", None),
    "completed": ("🏆", "COMPLETED", None),
}
_BROADCAST_TARGETS = ("announcements", "vc", "participants")


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
        # Cooldowns, hourly history and per-user stats all live in GambleBook
        # (SQLite meta, keyed by event): one source of truth that survives
        # restarts and never bleeds across events.
        self._gamble_book = GambleBook(engine.store)
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
        """`!` prefix commands work everywhere: DMs and server channels alike.

        They used to be DM-only, which made `!status`, `!help`, … silently do
        nothing when typed in the server — exactly where people tried them.
        """
        return True

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

    def _build_leaderboard_embeds(self, *, board: str = "real") -> list[discord.Embed]:
        if board == "gamble":
            entries = self.engine.gamble_leaderboard()
            title = "🎰 WELCOME TO HELL — GAMBLE TIME LEADERBOARD"
            embeds = self.announcer.build_leaderboard_live_embeds(entries, title=title)
            if embeds:
                embeds[-1].set_footer(text="Gamble Time only — not VC Real Timer. No rate limits on this clock.")
            return embeds
        entries = self.engine.leaderboard()
        frozen = self.engine.status.is_terminal
        title = "🏆 WELCOME TO HELL — FINAL LEADERBOARD" if frozen else "🏆 WELCOME TO HELL — LIVE LEADERBOARD"
        embeds = self.announcer.build_leaderboard_live_embeds(entries, title=title)
        if frozen and embeds:
            embeds[-1].set_footer(text="These rankings are frozen; the event is over.")
        return embeds

    def _in_vc_chat(self, channel_id: Optional[int]) -> bool:
        """Was a command used in the VC text chat (where the roll calls live)?"""
        if channel_id is None:
            return False
        vc_chat = self.config.alive_check_channel_id or self.config.voice_channel_id
        return channel_id == vc_chat

    def _build_difficulty_embed(self) -> discord.Embed:
        elapsed = self.engine.elapsed() if self.engine.is_running else 0.0
        return self.announcer.embeds.difficulty_info(elapsed, override=self.engine.difficulty_override)

    def _build_broadcast_embed(self, message: str, level: str) -> discord.Embed:
        """One colored embed: the host's text, colored by severity, no plain text."""
        meta = _BROADCAST_LEVELS.get(level, _BROADCAST_LEVELS["info"])
        emoji, label, _ = meta
        template = getattr(TEXT, f"BROADCAST_TITLE_{label}", None) or f"{emoji} {label}"
        title = say(template, level=label, emoji=emoji).strip()
        color_value = getattr(TEXT, f"COLOR_{label}", None)
        if color_value is None:
            color_value = getattr(TEXT, f"COLOR_{level.upper()}", 0xE25822)
        embedding = discord.Embed(
            title=title if title else None,
            description=message[:MAX_DESCRIPTION],
            color=int(color_value),
        )
        self.announcer.embeds._brand(embedding)
        return embedding

    # ------------------------------------------------- broadcast by DM (host)

    def _broadcast_roster(self) -> list[int]:
        """Who "all participants" means: every contestant with recorded time
        in the current event (the final-DM roster), falling back to whoever
        is in the VC right now."""
        board = self.engine.leaderboard()
        ids = [entry.user_id for entry in board]
        if not ids:
            ids = [p.user_id for p in self.engine.last_participants]
        return ids

    async def _broadcast_to_participants(self, message: str) -> BroadcastResult:
        ids = self._broadcast_roster()
        return await dm_participants(
            self.bot, ids, message, delay=self.config.dm_delay_seconds
        )

    def _dm_broadcast_summary(self, result: BroadcastResult) -> str:
        """One line for the host about what the DM broadcast actually did."""
        if result.total == 0:
            return str(
                getattr(
                    TEXT,
                    "CMD_BROADCAST_DM_EMPTY",
                    "📣 Nobody to DM: the event has no recorded participants yet.",
                )
            )
        if result.network_down:
            return say(
                getattr(
                    TEXT,
                    "CMD_BROADCAST_DM_NETWORK_DOWN",
                    "⚠️ **DM broadcast interrupted:** Discord stopped answering mid-way — "
                    "{delivered} of {total} received it. Send it again once the connection is back.",
                ),
                delivered=result.delivered,
                total=result.total,
            )
        blocked_note = f" · {result.blocked} with closed DMs" if result.blocked else ""
        failed_note = f", **{len(result.failed)}** could not be delivered" if result.failed else ""
        return say(
            getattr(
                TEXT,
                "CMD_BROADCAST_DM_DONE",
                "📣 **DM broadcast complete.** {delivered} of {total} participants received the message{blocked_note}{failed_note}.",
            ),
            delivered=result.delivered,
            total=result.total,
            blocked_note=blocked_note,
            failed_note=failed_note,
        )

    async def _handle_set_difficulty(self, level_input: str) -> tuple[bool, str]:
        clean = level_input.strip().lower()
        if clean in ("auto", "none", "clear", "reset", "default"):
            diff = self.engine.set_difficulty_override(None)
            self.monitor.sync_status()
            return True, say(
                getattr(TEXT, "CMD_SETDIFFICULTY_AUTO", "⚡ Difficulty override cleared — difficulty is now managed **automatically** based on event progress (currently **Level {level}: {name}**)."),
                level=diff.level,
                name=diff.name,
            )
        try:
            lvl = int(clean)
            if not (0 <= lvl <= 4):
                return False, "❌ Difficulty level must be between 0 and 4, or `auto`."
        except ValueError:
            return False, "❌ Difficulty level must be an integer 0–4 (e.g. `0`, `1`, `2`, `3`, `4`) or `auto`."

        diff = self.engine.set_difficulty_override(lvl)
        self.monitor.sync_status()
        return True, say(
            getattr(TEXT, "CMD_SETDIFFICULTY_DONE", "⚡ Difficulty set to **Level {level} ({name})**.\n• {description}"),
            level=diff.level,
            name=diff.name,
            description=diff.description,
        )

    async def _handle_announce_difficulty(self, overview: bool = False) -> tuple[bool, str]:
        from .difficulty import get_difficulty
        diff = get_difficulty(self.engine.elapsed(), override=self.engine.difficulty_override)
        # Report the channel the announcer really uses (an in-flight event may
        # carry its own announcement channel, overriding the .env value).
        chan_id = self.engine.state.announce_channel_id or self.config.announce_channel_id
        if overview:
            msg = await self.announcer.announce_difficulty_overview()
        else:
            msg = await self.announcer.announce_difficulty(diff)
        if msg is None:
            return False, f"❌ Failed to post announcement to <#{chan_id}> (channel missing or forbidden)."
        return True, say(
            getattr(TEXT, "CMD_ANNOUNCE_DIFFICULTY_DONE", "📢 Difficulty announcement posted to {channel}."),
            channel=f"<#{chan_id}>",
        )

    def _build_odds_embed(self) -> discord.Embed:
        from .difficulty import get_difficulty

        diff = get_difficulty(self.engine.elapsed(), override=self.engine.difficulty_override)
        return self.announcer.embeds.gamble_odds(diff)

    def _gamble_stats_line(self, stats: GambleStats, *, clock: str, left_bets: Optional[int]) -> str:
        """One-line session summary appended to every gamble result."""
        sign = "+" if stats.net_seconds >= 0 else "-"
        net = format_hm(abs(stats.net_seconds))
        bets = f"{stats.bets} bet{'s' if stats.bets != 1 else ''}"
        line = f"📊 This run: {bets} · {stats.wins}W/{stats.losses}L · net {sign}{net}"
        if clock == "real" and left_bets is not None:
            left = f"{left_bets} fast bet{'s' if left_bets != 1 else ''} left this hour"
            line += f" · ⏱️ {left}"
        return line

    async def _perform_gamble(
        self, user: Any, hours: Optional[Union[float, str]] = None, *, clock: str = "real"
    ) -> tuple[bool, str]:
        if not self.engine.is_running:
            return False, say(TEXT.CMD_GAMBLE_NOT_RUNNING, status=self.engine.status.value)
        if self.engine.is_paused:
            return False, TEXT.CMD_GAMBLE_PAUSED
        if self.engine.grace.is_open:
            return False, str(
                getattr(
                    TEXT,
                    "CMD_GAMBLE_GRACE",
                    "⚠️ Cannot gamble while the voice channel is empty — get someone back in first.",
                )
            )

        from .difficulty import get_difficulty
        diff = get_difficulty(self.engine.elapsed(), override=self.engine.difficulty_override)
        if not diff.gamble_enabled:
            return False, say(TEXT.CMD_GAMBLE_LOCKED, level=diff.level, name=diff.name)

        clock = (clock or "real").strip().lower()
        if clock in ("gamble", "gambletime", "casino"):
            clock = "gamble"
        else:
            clock = "real"
        clock_name = "Gamble Time" if clock == "gamble" else "Real Timer"

        bet_hours = parse_bet_hours(hours)
        if bet_hours is None:
            return False, say(
                getattr(
                    TEXT,
                    "CMD_GAMBLE_INVALID_BET",
                    "❌ Bet must be between **{min_hours}** and **{max_hours}h** for Difficulty {level} (e.g. `0.25`, `15m`, `1h`).",
                ),
                min_hours=f"{MIN_BET_HOURS * 60:.0f}m",
                max_hours=f"{diff.gamble_max_bet_hours:g}",
                level=diff.level,
            )
        bet_hours = snap_bet_hours(bet_hours)
        if bet_hours < MIN_BET_HOURS:
            return False, f"❌ Minimum bet is **{MIN_BET_HOURS * 60:.0f} minutes**."
        if bet_hours > diff.gamble_max_bet_hours:
            return False, say(
                getattr(
                    TEXT,
                    "CMD_GAMBLE_INVALID_BET",
                    "❌ Bet must be between **{min_hours}** and **{max_hours}h** for Difficulty {level} (e.g. `0.25`, `15m`, `1h`).",
                ),
                min_hours=f"{MIN_BET_HOURS * 60:.0f}m",
                max_hours=f"{diff.gamble_max_bet_hours:g}",
                level=diff.level,
            )

        user_id = user.id
        present_ids = {p.user_id for p in self.engine.last_participants}
        if user_id not in present_ids:
            return False, str(
                getattr(
                    TEXT,
                    "CMD_GAMBLE_NOT_IN_VC",
                    "❌ You must be **in the Hell voice channel** to gamble.",
                )
            )

        bet_seconds = bet_hours * 3600.0
        # A Gamble Time loss costs more than the stake (1.5×–2×), so the
        # wallet must cover the worst case; a Real Timer loss costs the stake.
        loss_mult = gamble_time_loss_multiplier(diff, bet_hours) if clock == "gamble" else 1.0
        needed = bet_seconds * loss_mult
        if clock == "gamble":
            user_time = self.engine.gamble_wallet(user_id)
        else:
            board = self.engine.leaderboard()
            user_entry = next((e for e in board if e.user_id == user_id), None)
            user_time = user_entry.seconds if user_entry else 0.0

        # Never let a player stake their entire clock (they must keep some time).
        if user_time <= needed:
            return False, say(
                TEXT.CMD_GAMBLE_NO_TIME,
                user_time=format_hm(user_time),
                min_time=format_hm(needed),
                clock_name=clock_name,
            )

        now = now_ts()
        event_uid = self.engine.event_uid or ""

        # Real Timer bets are rate limited: a fast-bet quota per hour, then a
        # separate overflow timer. Cooldowns and history live ONLY in the
        # GambleBook (SQLite, keyed by event) — one source of truth that
        # survives restarts and never leaks between events.
        fast_bets_left: Optional[int] = None
        if clock == "real":
            history = self._gamble_book.history(event_uid, user_id, now=now)
            last_gamble = self._gamble_book.last_ts(event_uid, user_id)
            cooldown, overflow = effective_cooldown(diff, len(history))
            if now - last_gamble < cooldown:
                wait = format_wait(cooldown - (now - last_gamble))
                if overflow:
                    return False, say(
                        getattr(
                            TEXT,
                            "CMD_GAMBLE_OVERFLOW",
                            "⏳ **Fast bets used** ({limit} this hour). Extra gambles use a **separate timer** — wait **{cooldown}**.",
                        ),
                        limit=diff.gamble_hourly_limit,
                        cooldown=wait,
                    )
                return False, say(TEXT.CMD_GAMBLE_COOLDOWN, cooldown=wait)
            self._gamble_book.record(event_uid, user_id, now)
            fast_bets_left = max(0, diff.gamble_hourly_limit - len(history) - 1)

        rolled = resolve_gamble(diff, bet_hours, roll=random.random())
        mention = getattr(user, "mention", f"<@{user_id}>")
        display_name = getattr(user, "display_name", None) or str(user)
        bet_time_str = format_hm(bet_seconds)
        shown_chance = round(rolled.win_chance * 100)

        if rolled.won:
            # The payout IS the credit: bet × multiplier is added on top of the
            # untouched stake, and the message shows exactly that.
            reward_sec = bet_seconds * rolled.multiplier
            if clock == "gamble":
                new_seconds = self.engine.add_gamble_seconds(
                    user_id, display_name, reward_sec, net_delta=reward_sec - bet_seconds
                )
            else:
                new_seconds = self.engine.add_user_bonus_seconds(user_id, display_name, reward_sec, now)
            stats = self._gamble_book.record_gamble(
                event_uid, user_id, bet_seconds=bet_seconds,
                won=True, jackpot=rolled.jackpot, won_seconds=reward_sec,
            )
            msg = say(
                TEXT.CMD_GAMBLE_JACKPOT if rolled.jackpot else TEXT.CMD_GAMBLE_WIN,
                who=mention,
                win_chance=shown_chance,
                level=diff.level,
                reward_time=format_hm(reward_sec),
                bet_time=bet_time_str,
                multiplier=f"{rolled.multiplier:g}",
                new_time=format_hm(new_seconds),
                clock_name=clock_name,
                stats_line=self._gamble_stats_line(stats, clock=clock, left_bets=fast_bets_left),
            )
            return True, msg
        else:
            penalty_sec = bet_seconds * loss_mult
            if clock == "gamble":
                new_seconds = self.engine.add_gamble_seconds(
                    user_id, display_name, -penalty_sec, net_delta=-penalty_sec
                )
            else:
                new_seconds = self.engine.add_user_bonus_seconds(user_id, display_name, -penalty_sec, now)
            stats = self._gamble_book.record_gamble(
                event_uid, user_id, bet_seconds=bet_seconds,
                won=False, lost_seconds=penalty_sec,
            )
            stats_line = self._gamble_stats_line(stats, clock=clock, left_bets=fast_bets_left)
            if clock == "real":
                mute_sec = rolled.mute_seconds
                if hasattr(self.monitor.alive_checks, "io") and hasattr(self.monitor.alive_checks.io, "mute"):
                    try:
                        await self.monitor.alive_checks.io.mute(user_id, mute_sec, "Welcome to Hell: lost gamble")
                    except Exception:
                        log.warning("Could not mute user %d after gamble loss", user_id, exc_info=True)
                lose_msg = say(
                    TEXT.CMD_GAMBLE_LOSE,
                    who=mention,
                    level=diff.level,
                    win_chance=shown_chance,
                    bet_time=bet_time_str,
                    penalty_time=format_hm(penalty_sec),
                    mute_duration=format_mute(mute_sec),
                    new_time=format_hm(new_seconds),
                    clock_name=clock_name,
                    stats_line=stats_line,
                )
            else:
                lose_msg = say(
                    getattr(
                        TEXT,
                        "CMD_GAMBLE_LOSE_WALLET",
                        "💀 **GAMBLE LOST!** 🎲 {who} rolled a LOSS ({win_chance}% win odds) on Difficulty {level}!\n\n"
                        "You lost **-{penalty_time}** from Gamble Time (heavier than the stake — **no mute**).\n"
                        "*Bet:* `{bet_time}` · *New Gamble Time:* `{new_time}`",
                    ),
                    who=mention,
                    level=diff.level,
                    win_chance=shown_chance,
                    bet_time=bet_time_str,
                    penalty_time=format_hm(penalty_sec),
                    new_time=format_hm(new_seconds),
                    stats_line=stats_line,
                )
            return True, lose_msg

    def _adjust_member_time(self, member: Any, hours_raw: str, clock: str) -> tuple[bool, str]:
        if not self.engine.event_uid:
            return False, "❌ No event is loaded."
        text = str(hours_raw).strip()
        negative = text.startswith("-")
        if negative:
            text = text[1:].strip()
        parsed = parse_bet_hours(text)
        if parsed is None:
            return False, "❌ Amount must look like `1`, `0.5`, `15m`, `-1h`."
        delta_hours = -parsed if negative else parsed
        delta_sec = delta_hours * 3600.0
        uid = member.id
        name = getattr(member, "display_name", None) or str(member)
        mention = getattr(member, "mention", f"<@{uid}>")
        clock = "gamble" if str(clock).lower().startswith("gamble") else "real"
        if clock == "gamble":
            new_total = self.engine.add_gamble_seconds(uid, name, delta_sec)
            label = "Gamble Time"
        else:
            new_total = self.engine.add_user_bonus_seconds(uid, name, delta_sec)
            label = "Real Timer"
        sign = "+" if delta_sec >= 0 else ""
        return True, (
            f"✅ {mention} **{label}** {sign}{format_hm(delta_sec)} → `{format_hm(new_total)}`."
        )

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
        embed = self.monitor.reports.build_embed(report)
        if self.engine.event_uid:
            self._add_gamble_section(embed, user_id)
        return embed, None

    def _add_gamble_section(self, embed: discord.Embed, user_id: int) -> None:
        """Append the player's gambling record (wallet + stats) to the card."""
        event_uid = self.engine.event_uid or ""
        wallet = self.engine.gamble_wallet(user_id)
        stats = self._gamble_book.stats(event_uid, user_id)
        if wallet <= 0 and stats.bets == 0:
            return
        if stats.bets > 0:
            net = stats.net_seconds
            sign = "+" if net >= 0 else "-"
            jackpots = (
                say(
                    getattr(TEXT, "CMD_MYSTATS_GAMBLE_JACKPOTS", " · {jackpots} 💎"),
                    jackpots=stats.jackpots,
                )
                if stats.jackpots
                else ""
            )
            value = say(
                getattr(
                    TEXT,
                    "CMD_MYSTATS_GAMBLE_LINES",
                    "• Wallet: **{wallet}** Gamble Time\n"
                    "• Bets: **{bets}** ({wins}W / {losses}L{jackpots})\n"
                    "• Net: **{net}**",
                ),
                wallet=format_hm(wallet),
                bets=stats.bets,
                wins=stats.wins,
                losses=stats.losses,
                jackpots=jackpots,
                net=f"{sign}{format_hm(abs(net))}",
            )
        else:
            value = say(
                getattr(TEXT, "CMD_MYSTATS_GAMBLE_NONE", "• No bets yet — Gamble Time wallet: **{wallet}**"),
                wallet=format_hm(wallet),
            )
        add_chunked_field(
            embed,
            str(getattr(TEXT, "CMD_MYSTATS_GAMBLE_FIELD", "🎰 Gambling")),
            value,
        )

    def _build_user_embed(self, user_id: int, display_name: str) -> tuple[Optional[discord.Embed], Optional[str]]:
        board = self.engine.leaderboard()
        entry = next((e for e in board if e.user_id == user_id), None)
        if entry is None:
            who_str = display_name if display_name.startswith("<@") else f"<@{user_id}>"
            return None, say(TEXT.CMD_USER_NO_TIME, who=who_str)

        elapsed = self.engine.elapsed()
        claimed = [
            record.hours
            for record in self.engine.milestone_records()
            if any(m.user_id == user_id for m in record.members)
        ]
        present = any(p.user_id == user_id for p in self.engine.last_participants)

        name_to_show = display_name
        if display_name.startswith("<@") or display_name.isdigit():
            name_to_show = entry.display_name or display_name
        embed = discord.Embed(
            title=say(TEXT.CMD_USER_TITLE, name=name_to_show),
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
        m = re.match(r"^(?:HEL-)?(\d+)$", code)
        if m:
            code = f"HEL-{m.group(1).zfill(3)}"
        elif not code.startswith("HEL-"):
            code = f"HEL-{code}"
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
                    f"• Elapsed: **{format_hm(self.engine.elapsed())}** / {format_hm(self.engine.state.total_seconds)}",
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

    def _is_host(self, user: Union[discord.User, discord.Member]) -> bool:
        if user.id == self.config.log_dm_user_id:
            return True
        if isinstance(user, discord.Member):
            return any(r.id == self.config.gamenight_host_role_id for r in user.roles)
        roles = getattr(user, "roles", None)
        if roles:
            return any(getattr(r, "id", None) == self.config.gamenight_host_role_id for r in roles)
        return False

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
        if self._in_vc_chat(interaction.channel_id):
            # In the VC text chat: link the pinned live status card instead of
            # dumping a copy of it — Discord renders the link preview.
            await interaction.followup.send(content=TEXT.CMD_STATUS_VC_LINK)
            return
        count = len(self.engine.last_participants)
        if self.engine.is_running:
            collected = await self.monitor.collect()
            if collected is not None:
                count = len(collected[0])
        embed = self._build_status_embed(count)
        await interaction.followup.send(embed=embed)

    # ----------------------------------------------------------- leaderboard

    @app_commands.command(name="leaderboard", description="Show Real Timer or Gamble Time rankings.")
    @app_commands.describe(board="Real Timer (VC time) or Gamble Time.")
    @app_commands.choices(
        board=[
            app_commands.Choice(name="Real Timer", value="real"),
            app_commands.Choice(name="Gamble Time", value="gamble"),
        ],
    )
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction, board: Optional[app_commands.Choice[str]] = None) -> None:
        await interaction.response.defer(thinking=True)
        which = board.value if board is not None else "real"
        if which != "gamble" and self._in_vc_chat(interaction.channel_id):
            await interaction.followup.send(content=TEXT.CMD_LEADERBOARD_VC_LINK)
            return
        embeds = self._build_leaderboard_embeds(board=which)
        msg = await interaction.followup.send(embeds=embeds, wait=True)

        frozen = self.engine.status.is_terminal
        if which == "real" and self.engine.is_running and not frozen:
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

    # ------------------------------------------------------------ difficulty

    @app_commands.command(
        name="difficulty",
        description="View difficulty tiers, set the current level, or post the tier to announcements.",
    )
    @app_commands.describe(
        action="What to do: view (everyone), set (host only) or announce (host only).",
        level="Difficulty level when action='set'.",
        overview="Post the full 5-tier overview instead of only the current tier (announce only).",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="👁️ View", value="view"),
            app_commands.Choice(name="⚙️ Set (host)", value="set"),
            app_commands.Choice(name="📢 Announce (host)", value="announce"),
        ],
        level=[
            app_commands.Choice(name="auto (Default based on elapsed time)", value="auto"),
            app_commands.Choice(name="Level 0: Starter (1-6h alive checks)", value="0"),
            app_commands.Choice(name="Level 1: Heating Up (1-5h alive checks)", value="1"),
            app_commands.Choice(name="Level 2: Inferno (1-4h checks + dead checks 1m mute)", value="2"),
            app_commands.Choice(name="Level 3: Torment (1-3h checks + dead checks + gambling)", value="3"),
            app_commands.Choice(name="Level 4: Cataclysm (1-2h checks + high-stakes gambling)", value="4"),
        ],
    )
    @app_commands.guild_only()
    async def difficulty(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] = None,
        level: app_commands.Choice[str] = None,
        overview: bool = False,
    ) -> None:
        act = (action.value if action is not None else "view").lower()
        if act == "view":
            embed = self._build_difficulty_embed()
            await interaction.response.send_message(embed=embed)
            return
        # Host-only actions defer and reply ephemerally.
        await interaction.response.defer(thinking=True, ephemeral=True)
        if act == "set":
            if not self._is_interaction_host(interaction):
                await interaction.followup.send(
                    say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"),
                    ephemeral=True,
                )
                return
            if level is None:
                await interaction.followup.send(TEXT.CMD_SETDIFFICULTY_LEVEL_REQUIRED, ephemeral=True)
                return
            _ok, text = await self._handle_set_difficulty(level.value)
            await interaction.followup.send(text, ephemeral=True)
            return
        if act == "announce":
            if not self._is_interaction_host(interaction):
                await interaction.followup.send(
                    say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"),
                    ephemeral=True,
                )
                return
            _ok, text = await self._handle_announce_difficulty(overview=overview)
            await interaction.followup.send(text, ephemeral=True)
            return
        embed = self._build_difficulty_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _is_interaction_host(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        roles = getattr(member, "roles", None)
        if roles is None:
            return False
        return any(r.id == self.config.gamenight_host_role_id for r in roles)

    @app_commands.command(name="broadcast", description="Embed to the announcement channel or VC chat, or a markdown DM to every participant.")
    @app_commands.describe(
        message="The host message to post (or DM, verbatim, for the participants target).",
        level="Broadcast colour/severity (info, warning, error, …). Default: info.",
        target="Where to send it: announcement channel, VC text chat, or every participant by DM. Default: announcements.",
    )
    @app_commands.choices(
        level=[
            app_commands.Choice(name="ℹ️ Info", value="info"),
            app_commands.Choice(name="✅ Success", value="success"),
            app_commands.Choice(name="⚠️ Warning", value="warning"),
            app_commands.Choice(name="🚨 Error", value="error"),
            app_commands.Choice(name="🐞 Debug", value="debug"),
            app_commands.Choice(name="🔥 Milestone", value="milestone"),
            app_commands.Choice(name="💤 Idle", value="idle"),
            app_commands.Choice(name="⏳ Grace", value="grace"),
            app_commands.Choice(name="🏆 Completed", value="completed"),
        ],
        target=[
            app_commands.Choice(name="Announcement channel", value="announcements"),
            app_commands.Choice(name="VC text chat", value="vc"),
            app_commands.Choice(name="All participants (DM each)", value="participants"),
        ],
    )
    @is_host()
    @app_commands.guild_only()
    async def broadcast(
        self,
        interaction: discord.Interaction,
        message: str,
        level: app_commands.Choice[str] = None,
        target: app_commands.Choice[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not self._is_interaction_host(interaction):
            await interaction.followup.send(
                say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"),
                ephemeral=True,
            )
            return
        lvl = (level.value if level is not None else "info").lower()
        tgt = (target.value if target is not None else "announcements")
        if tgt == "participants":
            result = await self._broadcast_to_participants(message)
            log.info(
                "DM broadcast by %s (%s): %d/%d delivered, %d blocked, %d failed%s",
                interaction.user, interaction.user.id, result.delivered, result.total,
                result.blocked, len(result.failed),
                " (network down)" if result.network_down else "",
            )
            await interaction.followup.send(self._dm_broadcast_summary(result), ephemeral=True)
            return
        embed = self._build_broadcast_embed(message, lvl)
        sent = await self.announcer.send([embed], target=tgt)
        if sent is None:
            await interaction.followup.send(
                say(TEXT.CMD_BROADCAST_FAILED, target=(TEXT.BROADCAST_TARGET_VC if tgt == "vc" else TEXT.BROADCAST_TARGET_ANNOUNCE)),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            say(
                TEXT.CMD_BROADCAST_DONE,
                target=(TEXT.BROADCAST_TARGET_VC if tgt == "vc" else TEXT.BROADCAST_TARGET_ANNOUNCE),
                level=lvl.upper(),
            ),
            ephemeral=True,
        )

    # -------------------------------------------------------------- gambling

    @app_commands.command(name="gamble", description="Gamble Real Timer (rate limited) or Gamble Time (no rate limits). Difficulty 3+.")
    @app_commands.describe(
        hours="Hours to bet (e.g. 0.25, 0.5, 1.0). Default: 0.25h (15m).",
        clock="Real Timer uses VC time + cooldowns. Gamble Time has no rate limits.",
    )
    @app_commands.choices(
        clock=[
            app_commands.Choice(name="Real Timer (rate limits)", value="real"),
            app_commands.Choice(name="Gamble Time (no rate limits)", value="gamble"),
        ],
    )
    @app_commands.guild_only()
    async def gamble(
        self,
        interaction: discord.Interaction,
        hours: Optional[float] = None,
        clock: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        which = clock.value if clock is not None else "real"
        _ok, msg = await self._perform_gamble(interaction.user, hours=hours, clock=which)
        await interaction.followup.send(msg)

    @app_commands.command(name="odds", description="Current gambling odds, payouts and limits (Difficulty 3+).")
    @app_commands.guild_only()
    async def odds(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await interaction.response.send_message(embed=self._build_odds_embed())

    @app_commands.command(name="adjtime", description="Host: add or remove Real Timer or Gamble Time for a member.")
    @app_commands.describe(
        member="Who to adjust.",
        hours="Hours to add (positive) or remove (negative), e.g. 1, -0.5, 15m.",
        clock="Real Timer (VC leaderboard) or Gamble Time wallet.",
    )
    @app_commands.choices(
        clock=[
            app_commands.Choice(name="Real Timer", value="real"),
            app_commands.Choice(name="Gamble Time", value="gamble"),
        ],
    )
    @is_host()
    @app_commands.guild_only()
    async def adjtime(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        hours: str,
        clock: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        _ok, text = self._adjust_member_time(member, hours, clock.value if clock else "real")
        await interaction.followup.send(text, ephemeral=True)

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
        description="Run an alive check right now (normally random every 1-6h). DM-only, hosts.",
    )
    @dm_host_only()
    async def alivecheck(self, interaction: discord.Interaction) -> None:
        # DM-only: no ephemeral flags (Discord rejects them outside a guild).
        await interaction.response.defer(thinking=True)
        if not self.engine.is_running:
            await interaction.followup.send(
                say(TEXT.CMD_ALIVECHECK_NO_EVENT, status=self.engine.status.value)
            )
            return
        if not self.config.alive_check_enabled:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_DISABLED)
            return
        if self.engine.is_paused:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_PAUSED)
            return
        if self.monitor.alive_checks.pending is not None:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_ALREADY)
            return
        started = await self.monitor.force_alive_check()
        if not started:
            await interaction.followup.send(TEXT.CMD_ALIVECHECK_FAILED)
            return
        await interaction.followup.send(
            say(
                TEXT.CMD_ALIVECHECK_STARTED,
                check_channel=f"<#{self.monitor.alive_io.channel_id()}>",
                minutes=int(self.config.alive_check_timeout_minutes),
            )
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
        # DM-only command: ephemeral replies are rejected outside a guild.
        await interaction.response.send_message(TEXT.CMD_RESTART_DONE)
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
        description="Unfreeze after /hell pause, continue a failed run, or start Hell 2 after the 320h vote passes.",
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

        if self.engine.status is EventStatus.COMPLETED:
            await interaction.response.defer(thinking=True, ephemeral=True)
            if not self.monitor.continuation.yes_won():
                await interaction.followup.send(TEXT.CMD_CONTINUATION_NOT_ALLOWED, ephemeral=True)
                return
            try:
                async with self.monitor.lock:
                    self.engine.resume_continuation(approved=True)
            except StartError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            if self.monitor.alive_checks.pending is not None:
                await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
            self.announcer.forget_progress_message()
            count = len(self.engine.last_participants)
            snap = self.engine.snapshot(participants=count)
            await self.announcer.update_progress(snap, force=True)
            await self.announcer.announce_continuation_resume()
            log.warning("COMPLETED event RESUMED as Hell 2 by %s (%s)", interaction.user, interaction.user.id)
            self.monitor.sync_status()
            await interaction.followup.send(TEXT.CMD_CONTINUATION_DONE, ephemeral=True)
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
            # A 4xx from Discord on the *response* itself (not on our data)
            # usually means the interaction was already answered or has
            # expired — the classic symptom of a second bot instance running
            # with the same token. Say so explicitly, with identifiers that
            # make it greppable in the logs.
            if isinstance(error, discord.HTTPException):
                status = getattr(error, "status", None)
                if status in (400, 403, 404, 405):
                    log.error(
                        "Discord rejected the response for /hell %s (HTTP %s): "
                        "the interaction was probably already answered — is a "
                        "second copy of the bot running? interaction=%s guild=%s",
                        interaction.command.name if interaction.command else "?",
                        status,
                        getattr(interaction, "id", "?"),
                        getattr(interaction, "guild_id", "?"),
                    )
        if isinstance(error, DMsClosed):
            message = TEXT.CMD_DM_ONLY
        elif isinstance(error, NotOperator):
            message = TEXT.CMD_OPERATOR_ONLY
        # Ephemeral replies are rejected in DMs — only use them inside a guild.
        ephemeral = interaction.guild is not None
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except discord.HTTPException:
            pass

    # =========================================================================
    # Prefix Commands (for DMs and chat channels: !status, !help, !hell ...)
    # =========================================================================

    async def _exec_status(self, ctx: commands.Context) -> None:
        if self._in_vc_chat(getattr(ctx.channel, "id", None)):
            await ctx.send(TEXT.CMD_STATUS_VC_LINK)
            return
        count = len(self.engine.last_participants)
        if self.engine.is_running:
            collected = await self.monitor.collect()
            if collected is not None:
                count = len(collected[0])
        embed = self._build_status_embed(count)
        await ctx.send(embed=embed)

    async def _exec_leaderboard(self, ctx: commands.Context, board: Optional[str] = None) -> None:
        which = "gamble" if board and str(board).lower().startswith("gamble") else "real"
        if which != "gamble" and self._in_vc_chat(getattr(ctx.channel, "id", None)):
            await ctx.send(TEXT.CMD_LEADERBOARD_VC_LINK)
            return
        embeds = self._build_leaderboard_embeds(board=which)
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

    async def _exec_difficulty(self, ctx: commands.Context) -> None:
        embed = self._build_difficulty_embed()
        await ctx.send(embed=embed)

    async def _exec_setdifficulty(self, ctx: commands.Context, level: Optional[str] = None) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not level:
            await ctx.send("Please specify a difficulty level (`0`, `1`, `2`, `3`, `4`, or `auto`).")
            return
        _ok, text = await self._handle_set_difficulty(level)
        await ctx.send(text)

    async def _exec_announcedifficulty(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        overview = bool(mode and mode.strip().lower() in ("overview", "all", "full"))
        _ok, text = await self._handle_announce_difficulty(overview=overview)
        await ctx.send(text)

    async def _exec_gamble(self, ctx: commands.Context, hours: Optional[str] = None) -> None:
        if hours is not None and str(hours).strip().lower() == "odds":
            await ctx.send(embed=self._build_odds_embed())
            return
        clock = "real"
        stake = hours
        if hours:
            parts = str(hours).split()
            if parts and parts[-1].lower() in ("real", "gamble", "gambletime", "casino"):
                last = parts[-1].lower()
                clock = "gamble" if last.startswith("gamble") or last == "casino" else "real"
                stake = " ".join(parts[:-1]) or None
            if stake is not None and parse_bet_hours(stake) is None:
                await ctx.send("❌ Bet amount must be a number of hours (e.g. `0.25`, `15m`, `1h`).")
                return
        _ok, msg = await self._perform_gamble(ctx.author, hours=stake, clock=clock)
        await ctx.send(msg)

    async def _exec_adjtime(self, ctx: commands.Context, rest: Optional[str] = None) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        parts = (rest or "").split()
        if len(parts) < 2:
            await ctx.send("Usage: `!adjtime @user <hours> [real|gamble]` (negative hours remove time).")
            return
        clock = "real"
        if parts[-1].lower() in ("real", "gamble", "gambletime"):
            clock = "gamble" if parts[-1].lower().startswith("gamble") else "real"
            parts = parts[:-1]
        if len(parts) < 2:
            await ctx.send("Usage: `!adjtime @user <hours> [real|gamble]`.")
            return
        who, amount = parts[0], parts[1]
        member_id = None
        m = re.match(r"^<@!?(\d+)>$", who)
        if m:
            member_id = int(m.group(1))
        elif who.isdigit():
            member_id = int(who)
        if member_id is None:
            await ctx.send("❌ Mention a user or pass their ID.")
            return
        fake = type("U", (), {})()
        fake.id = member_id
        fake.display_name = who
        fake.mention = f"<@{member_id}>"
        _ok, text = self._adjust_member_time(fake, amount, clock)
        await ctx.send(text)

    async def _exec_help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="🔥 Welcome to Hell — Commands",
            description=(
                "Use `!<command>` here in the server, in DMs, or `/hell <command>` anywhere.\n"
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
                "`!leaderboard [real|gamble]` — Real Timer or Gamble Time rankings",
                "`!mystats` (or `!mycard`, `!card`, `!stats`, `!me`) — Your personal stat card",
                "`!user [@user/id/name]` — Look up anyone's time, rank & milestones",
                "`!milestones` (or `!ms`) — Milestones, rewards & list of claimants",
                "`!difficulty` (or `!diff`) — View the 5 difficulty tiers and current level",
                "`!gamble [hours] [real|gamble]` — Bet Real Timer (rate limits) or Gamble Time (none)",
                "`!odds` (or `!gamble odds`) — Current gambling odds, payouts & limits",
                "`!errors <code>` — Look up an error code explanation (e.g. `!errors HEL-100`)",
                "`!help` — Show this command help list",
            ]),
        )
        add_chunked_field(
            embed,
            say("👑 Host & Operator Commands ({host_role} / Operator)", host_role=f"<@&{self.config.gamenight_host_role_id}>"),
            "\n".join([
                "`!adjtime @user <hours> [real|gamble]` — Add/remove Real Timer or Gamble Time",
                "`!setdifficulty <0-4|auto>` — Set or override difficulty level",
                "`!broadcast <info|warning|error|...> <announcements|vc|participants> <message>` — Colored embed, or a DM to every participant",
                "`!announcedifficulty [overview]` — Broadcast difficulty update to announcement channel",
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
                "`!dump` — (Operator only in DMs) Export the whole database: restorable SQL + human recap",
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

    async def _exec_dump(self, ctx: commands.Context) -> None:
        """DM-only, operator-only: send the full database dump + a human recap."""
        if ctx.guild is not None:
            # DM-only: a whole-database export must not land in a public channel.
            await ctx.send(TEXT.CMD_DM_ONLY)
            return
        if not self._is_operator(ctx):
            await ctx.send(TEXT.CMD_OPERATOR_ONLY)
            return
        from . import dbdump

        path = self.engine.store.path
        if str(path) == ":memory:" or not path.exists():
            await ctx.send("❌ No database file to dump (in-memory store?).")
            return
        try:
            sql_bytes = dbdump.dump_database(path)
            recap_bytes = dbdump.build_recap(path).encode("utf-8")
        except Exception:
            log.exception("Database dump failed")
            await ctx.send("❌ The database dump failed — the error is in the bot logs.")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        files = [
            discord.File(io.BytesIO(sql_bytes), filename=f"hellbot-database-{stamp}.sql"),
            discord.File(io.BytesIO(recap_bytes), filename=f"hellbot-recap-{stamp}.txt"),
        ]
        await ctx.send(
            say(
                TEXT.CMD_DUMP_DONE,
                sql_kb=f"{len(sql_bytes) / 1024:.0f}",
                recap_kb=f"{len(recap_bytes) / 1024:.1f}",
            ),
            files=files,
        )
        log.info(
            "Database dumped to the operator DM by %s (%s): %d KB SQL + %d KB recap",
            ctx.author, getattr(ctx.author, "id", "?"),
            len(sql_bytes) // 1024, len(recap_bytes) // 1024,
        )

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
        if ctx.guild is not None:
            # DM-only: forcing a roll call must not sit in a public channel.
            await ctx.send(TEXT.CMD_DM_ONLY)
            return
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

    async def _exec_broadcast(
        self, ctx: commands.Context, level: str = "info", target: str = "announcements", message: str = ""
    ) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if not message.strip():
            await ctx.send(TEXT.CMD_BROADCAST_NEED_MESSAGE)
            return
        lvl = level.lower()
        if lvl not in _BROADCAST_LEVELS:
            lvl = "info"
        tgt = str(target).strip().lower()
        if tgt in ("participants", "dm", "dms", "all"):
            tgt = "participants"
        if tgt not in _BROADCAST_TARGETS:
            tgt = "announcements"
        if tgt == "participants":
            result = await self._broadcast_to_participants(message.strip())
            log.info(
                "DM broadcast (prefix) by %s (%s): %d/%d delivered, %d blocked, %d failed%s",
                ctx.author, ctx.author.id, result.delivered, result.total,
                result.blocked, len(result.failed),
                " (network down)" if result.network_down else "",
            )
            await ctx.send(self._dm_broadcast_summary(result))
            return
        embed = self._build_broadcast_embed(message.strip(), lvl)
        sent = await self.announcer.send([embed], target=tgt)
        if sent is None:
            await ctx.send(
                say(TEXT.CMD_BROADCAST_FAILED, target=(TEXT.BROADCAST_TARGET_VC if tgt == "vc" else TEXT.BROADCAST_TARGET_ANNOUNCE))
            )
            return
        await ctx.send(
            say(
                TEXT.CMD_BROADCAST_DONE,
                target=(TEXT.BROADCAST_TARGET_VC if tgt == "vc" else TEXT.BROADCAST_TARGET_ANNOUNCE),
                level=lvl.upper(),
            )
        )

    async def _exec_resume(self, ctx: commands.Context) -> None:
        if not await self._is_host_or_operator(ctx):
            await ctx.send(say(TEXT.CMD_NOT_ALLOWED, host_role=f"<@&{self.config.gamenight_host_role_id}>"))
            return
        if self.engine.status is EventStatus.FAILED:
            if not await self._request_approval_dm(ctx, "resume"):
                return
            await ctx.send(TEXT.CMD_APPROVAL_REQUESTED)
            return
        if self.engine.status is EventStatus.COMPLETED:
            if not self.monitor.continuation.yes_won():
                await ctx.send(TEXT.CMD_CONTINUATION_NOT_ALLOWED)
                return
            try:
                async with self.monitor.lock:
                    self.engine.resume_continuation(approved=True)
            except StartError as exc:
                await ctx.send(f"❌ {exc}")
                return
            if self.monitor.alive_checks.pending is not None:
                await self.monitor.alive_checks.cancel(now_ts(), "the event was resumed")
            self.announcer.forget_progress_message()
            count = len(self.engine.last_participants)
            snap = self.engine.snapshot(participants=count)
            await self.announcer.update_progress(snap, force=True)
            await self.announcer.announce_continuation_resume()
            log.warning("COMPLETED event RESUMED as Hell 2 by %s (%s) via prefix", ctx.author, ctx.author.id)
            self.monitor.sync_status()
            await ctx.send(TEXT.CMD_CONTINUATION_DONE)
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
            await self._exec_leaderboard(ctx, board=rest)
        elif sub in ("adjtime", "addtime", "settime"):
            await self._exec_adjtime(ctx, rest=rest)
        elif sub in ("milestones", "ms"):
            await self._exec_milestones(ctx)
        elif sub in ("difficulty", "diff"):
            await self._exec_difficulty(ctx)
        elif sub in ("setdifficulty", "setdiff"):
            await self._exec_setdifficulty(ctx, level=rest)
        elif sub in ("announcedifficulty", "announcediff"):
            await self._exec_announcedifficulty(ctx, mode=rest)
        elif sub == "broadcast":
            parts = rest.split(maxsplit=2) if rest else []
            lvl = parts[0] if parts else "info"
            tgt = parts[1] if len(parts) > 1 else "announcements"
            msg = parts[2] if len(parts) > 2 else ""
            await self._exec_broadcast(ctx, level=lvl, target=tgt, message=msg)
        elif sub in ("gamble", "bet"):
            await self._exec_gamble(ctx, hours=rest)
        elif sub == "odds":
            await ctx.send(embed=self._build_odds_embed())
        elif sub in ("mystats", "stats", "me", "mycard", "card"):
            await self._exec_mystats(ctx)
        elif sub in ("user", "whois", "profile"):
            await self._exec_user(ctx, member=rest)
        elif sub in ("errors", "error", "err"):
            await self._exec_errors(ctx, code=rest)
        elif sub in ("help", "h", "commands"):
            await self._exec_help(ctx)
        elif sub == "restart":
            await self._exec_restart(ctx)
        elif sub == "dump":
            await self._exec_dump(ctx)
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
    async def prefix_leaderboard(self, ctx: commands.Context, *, board: Optional[str] = None) -> None:
        """Show Real Timer or Gamble Time rankings."""
        await self._exec_leaderboard(ctx, board=board)

    @commands.command(name="milestones", aliases=["ms"])
    async def prefix_milestones(self, ctx: commands.Context) -> None:
        """Show every milestone, its reward and who claimed it."""
        await self._exec_milestones(ctx)

    @commands.command(name="difficulty", aliases=["diff"])
    async def prefix_difficulty(self, ctx: commands.Context) -> None:
        """Show the 5 difficulty tiers and current challenge level."""
        await self._exec_difficulty(ctx)

    @commands.command(name="setdifficulty", aliases=["setdiff"])
    async def prefix_setdifficulty(self, ctx: commands.Context, level: Optional[str] = None) -> None:
        """Set the difficulty level (0-4 or auto)."""
        await self._exec_setdifficulty(ctx, level=level)

    @commands.command(name="announcedifficulty", aliases=["announcediff"])
    async def prefix_announcedifficulty(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        """Post difficulty update to the announcement channel."""
        await self._exec_announcedifficulty(ctx, mode=mode)

    @commands.command(name="gamble", aliases=["bet"])
    async def prefix_gamble(self, ctx: commands.Context, *, hours: Optional[str] = None) -> None:
        """Gamble Real Timer or Gamble Time (Difficulty 3+). `!gamble odds` shows the odds."""
        await self._exec_gamble(ctx, hours=hours)

    @commands.command(name="odds")
    async def prefix_odds(self, ctx: commands.Context) -> None:
        """Current gambling odds, payouts and limits."""
        await ctx.send(embed=self._build_odds_embed())

    @commands.command(name="adjtime", aliases=["addtime", "settime"])
    async def prefix_adjtime(self, ctx: commands.Context, *, rest: Optional[str] = None) -> None:
        """Host: add or remove Real Timer or Gamble Time."""
        await self._exec_adjtime(ctx, rest=rest)

    @commands.command(name="mystats", aliases=["stats", "me"])
    async def prefix_mystats(self, ctx: commands.Context) -> None:
        """Your personal Welcome to Hell stat card."""
        await self._exec_mystats(ctx)

    @commands.command(name="mycard", aliases=["card"])
    async def prefix_mycard(self, ctx: commands.Context) -> None:
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

    @commands.command(name="dump")
    async def prefix_dump(self, ctx: commands.Context) -> None:
        """Export the whole bot database: restorable SQL + human recap (operator DM only)."""
        await self._exec_dump(ctx)

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
        """Trigger an immediate alive check. DM only (operator or host)."""
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
