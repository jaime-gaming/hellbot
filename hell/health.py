"""Startup preflight checks.

Catches the misconfigurations that would otherwise only show up hours into a
run: wrong IDs, a missing Move Members permission (so `@clanker` users could
never be kicked), no Mention Everyone (so milestone pings silently fail), and
so on.  Results are logged *and* returned so the desktop launcher can show
them in a popup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import discord

from .config import Config

log = logging.getLogger("hell.health")


@dataclass
class HealthReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_text(self) -> str:
        lines = []
        lines += [f"ERROR   {e}" for e in self.errors]
        lines += [f"WARNING {w}" for w in self.warnings]
        lines += [f"OK      {i}" for i in self.info]
        return "\n".join(lines) or "No checks were run."

    def log(self) -> None:
        for item in self.errors:
            log.error("Preflight: %s", item)
        for item in self.warnings:
            log.warning("Preflight: %s", item)
        for item in self.info:
            log.info("Preflight: %s", item)


def _role_name(guild: discord.Guild, role_id: Optional[int]) -> Optional[str]:
    if not role_id:
        return None
    role = guild.get_role(role_id)
    return role.name if role else None


async def preflight(bot: discord.Client, config: Config) -> HealthReport:
    """Validate guild, channels, roles and permissions.  Never raises."""
    report = HealthReport()

    guild = bot.get_guild(config.guild_id)
    if guild is None:
        report.errors.append(f"HEL-002 Guild {config.guild_id} not found — is GUILD_ID correct and the bot invited to it?"
        )
        return report
    report.info.append(f"Connected to guild '{guild.name}' ({guild.id})")

    me = guild.me
    if me is None:  # pragma: no cover - defensive
        report.errors.append("Could not resolve the bot's own member object in the guild.")
        return report

    if not bot.intents.members:
        report.errors.append("[HEL-010] The Server Members intent is disabled — the bot cannot read who is in the VC. "
            "Enable it in the Discord Developer Portal."
        )
    if not bot.intents.voice_states:
        report.errors.append("[HEL-010] The Voice States intent is disabled — VC monitoring cannot work.")
    if not bot.intents.message_content:
        report.errors.append("[HEL-010] The Message Content intent is disabled — alive-check replies ('Yes') cannot be "
            "read and every roll call would end by disconnecting everyone. Enable 'Message "
            "Content Intent' in the Developer Portal (Bot -> Privileged Gateway Intents)."
        )

    # --- voice channel -----------------------------------------------------
    vc = guild.get_channel(config.voice_channel_id)
    if vc is None:
        report.errors.append(
            f"Voice channel {config.voice_channel_id} not found in this guild (check VOICE_CHANNEL_ID)."
        )
    elif not isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
        report.errors.append(f"Channel {config.voice_channel_id} is '{vc.name}', which is not a voice channel.")
    else:
        report.info.append(f"Target VC: '{vc.name}' ({vc.id}), {len(vc.members)} member(s) inside right now")
        perms = vc.permissions_for(me)
        if not perms.view_channel:
            report.errors.append(f"Missing 'View Channel' on '{vc.name}' — the bot is blind to the VC.")
        if not perms.move_members:
            report.errors.append(
                f"Missing 'Move Members' on '{vc.name}' — @clanker users cannot be disconnected."
            )

    # --- announcement channel ---------------------------------------------
    ann = guild.get_channel(config.announce_channel_id)
    if ann is None:
        report.errors.append(
            f"Announcement channel {config.announce_channel_id} not found (check ANNOUNCE_CHANNEL_ID)."
        )
    elif not isinstance(ann, discord.abc.Messageable):
        report.errors.append(f"Announcement channel {config.announce_channel_id} cannot receive messages.")
    else:
        report.info.append(f"Announcements go to '#{getattr(ann, 'name', ann.id)}'")
        perms = ann.permissions_for(me)  # type: ignore[arg-type]
        for flag, label, fatal in (
            (perms.view_channel, "View Channel", True),
            (perms.send_messages, "Send Messages", True),
            (perms.embed_links, "Embed Links", True),
            (perms.read_message_history, "Read Message History", False),
            (perms.mention_everyone, "Mention @everyone", False),
            (perms.manage_messages, "Manage Messages (to pin the progress message)", False),
        ):
            if flag:
                continue
            message = f"Missing '{label}' in the announcement channel."
            (report.errors if fatal else report.warnings).append(message)

    # --- alive-check channel ------------------------------------------------
    if config.alive_check_enabled:
        check_id = config.alive_check_channel_id or config.voice_channel_id
        chan = guild.get_channel(check_id)
        if chan is None:
            report.errors.append(
                f"Alive-check channel {check_id} not found (ALIVE_CHECK_CHANNEL_ID)."
            )
        elif not isinstance(chan, discord.abc.Messageable):
            report.errors.append(
                f"Alive-check channel {check_id} cannot receive messages — pick a text channel "
                "or a voice channel with text chat enabled."
            )
        else:
            where = "the VC's own text chat" if check_id == config.voice_channel_id else f"#{chan.name}"
            report.info.append(f"Alive checks post in {where} every "
                               f"{config.alive_check_min_hours:g}-{config.alive_check_max_hours:g}h")
            perms = chan.permissions_for(me)  # type: ignore[arg-type]
            if not perms.send_messages:
                report.errors.append(f"Missing 'Send Messages' in the alive-check channel ({check_id}).")
            if not perms.read_message_history:
                report.warnings.append(
                    "Missing 'Read Message History' in the alive-check channel — replies sent while "
                    "the bot is restarting cannot be recovered."
                )
            if not perms.add_reactions:
                report.warnings.append(
                    "Missing 'Add Reactions' in the alive-check channel — answers will not be ticked."
                )

    # --- roles -------------------------------------------------------------
    host = _role_name(guild, config.gamenight_host_role_id)
    if host is None:
        report.errors.append(
            f"Role {config.gamenight_host_role_id} (GAMENIGHT_HOST_ROLE_ID) does not exist — "
            "nobody would be able to start the event."
        )
    else:
        report.info.append(f"Host role: @{host}")

    clanker = _role_name(guild, config.clanker_role_id)
    if clanker is None:
        report.errors.append(f"Role {config.clanker_role_id} (CLANKER_ROLE_ID) does not exist.")
    else:
        report.info.append(f"Clanker role: @{clanker}")

    for label, role_id in (
        ("HELL_ROLE_ID", config.hell_role_id),
        ("HELLIST_ROLE_ID", config.hellist_role_id),
        ("HELL_MASTER_ROLE_ID", config.hell_master_role_id),
        ("COOL_PEOPLE_ROLE_ID", config.cool_people_role_id),
    ):
        if role_id and _role_name(guild, role_id) is None:
            report.warnings.append(
                f"{label}={role_id} does not match any role; rewards will be shown as plain text."
            )

    # --- operator DM (live log stream) -------------------------------------
    if config.log_dm_enabled:
        stream = getattr(bot, "log_stream", None)
        if stream is not None and stream.disabled_reason:
            report.warnings.append(
                f"Live log stream is disabled: {stream.disabled_reason}. "
                "The operator (LOG_DM_USER_ID) will not receive alerts or log messages."
            )
        if stream is not None and stream.running:
            try:
                user = bot.get_user(config.log_dm_user_id) or await bot.fetch_user(config.log_dm_user_id)
                dm = user.dm_channel or await user.create_dm()
                # A quick test: try fetching the DM channel's history (1 message)
                # to see if the operator has DMs open.  This is a read-only probe
                # that leaves no trace.
                async for _ in dm.history(limit=1):
                    break
                report.info.append(
                    f"Operator DM stream → @{user.name} ({config.log_dm_user_id}) — DMs appear open"
                )
            except discord.Forbidden:
                report.warnings.append(
                    f"Operator {config.log_dm_user_id} has DMs closed — "
                    "the live log stream will fail."
                )
            except discord.HTTPException as exc:
                report.warnings.append(
                    f"Could not verify operator DMs ({config.log_dm_user_id}): {exc}"
                )

    return report
