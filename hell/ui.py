"""Interaction widgets and permission checks for the `/hell` commands.

Kept apart from :mod:`hell.cog` so the command bodies stay readable: this file
holds the host check, the confirmation view and the reset modal — the bits that
guard destructive actions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from .config import Config
from .texts import TEXT, say

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cog import HellCommands

log = logging.getLogger("hell.ui")

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

    def __init__(self, cog: HellCommands):
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
