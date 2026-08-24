"""Interaction widgets and permission checks for the `/hell` commands.

Kept apart from :mod:`hell.cog` so the command bodies stay readable: this file
holds the host check and the approval-code gate that guards destructive actions
(`/hell stop`, `/hell reset`, `/hell resume` for failed runs).
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands

from .config import Config
from .texts import TEXT, say

log = logging.getLogger("hell.ui")

# Approval codes use an alphabet with no confusable characters (no 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LIFETIME_SECONDS = 300.0   # a code is valid for five minutes


def _random_code(length: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


@dataclass
class _PendingCode:
    action: str
    code: str
    issued_at: float
    event_uid: Optional[str] = None   # the event the approval was issued for


class CodeGate:
    """One-time approval codes for dangerous commands.

    A host asks for a destructive action (``/hell stop``, ``/hell reset``,
    ``/hell resume`` for a failed run); the code is delivered **by DM to the
    operator** (the account that receives the live log stream) and the action
    only runs after the host enters it with ``/hell approve``.  A code is
    single-use and expires after five minutes, so a screenshot of a DM is
    useless shortly afterwards.  Only one code is pending at a time;
    requesting a new one invalidates the previous.
    """

    def __init__(self) -> None:
        self._pending: Optional[_PendingCode] = None
        self._expired_action: Optional[str] = None

    @property
    def pending_action(self) -> Optional[str]:
        """The action waiting for approval, or None when nothing is pending."""
        self._expire_if_stale()
        return self._pending.action if self._pending is not None else None

    @property
    def pending_event_uid(self) -> Optional[str]:
        """The event the pending approval was issued for (stale-code guard)."""
        self._expire_if_stale()
        return self._pending.event_uid if self._pending is not None else None

    @property
    def expired_action(self) -> Optional[str]:
        """The action whose code expired (so the user can be told to re-run)."""
        self._expire_if_stale()
        return self._expired_action

    def issue(self, action: str, *, event_uid: Optional[str] = None) -> str:
        """Create a fresh code for `action`, invalidating any previous one.

        `event_uid` binds the code to the event it was requested for, so a
        stale code can never be redeemed against a *different* running event.
        """
        self._pending = _PendingCode(
            action=action, code=_random_code(), issued_at=time.time(), event_uid=event_uid
        )
        self._expired_action = None
        return self._pending.code

    def invalidate(self) -> None:
        """Cancel the pending code (e.g. it never reached the operator's DMs)."""
        self._pending = None
        self._expired_action = None

    def redeem(self, action: str, code: str) -> Optional[str]:
        """Consume the pending code.  None = approved; str = why it was refused."""
        self._expire_if_stale()
        pending = self._pending
        if pending is None:
            return "nothing is waiting for approval"
        if pending.action != action:
            return f"that code belongs to a different pending action ({pending.action!r})"
        if not secrets.compare_digest(pending.code.upper(), code.strip().upper()):
            return "that code is not correct"
        self._pending = None
        return None

    def _expire_if_stale(self) -> None:
        if (
            self._pending is not None
            and time.time() - self._pending.issued_at > CODE_LIFETIME_SECONDS
        ):
            self._expired_action = self._pending.action
            self._pending = None


class NotAHost(app_commands.CheckFailure):
    """Raised when a non-host tries to run a restricted command."""


class DMsClosed(app_commands.CheckFailure):
    """Raised when a command that needs DMs is used in a guild."""


class NotOperator(app_commands.CheckFailure):
    """Raised when a non-operator tries a DM-only operator command."""


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


def dm_operator_only():
    """Restrict a command to the operator (LOG_DM_USER_ID) via DM only.

    Only the bot operator can trigger a restart, and only from inside a DM
    session where the bot can confirm the user's identity without needing a
    guild role check.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is not None:
            raise DMsClosed("This command can only be used in DMs.")
        config: Config = interaction.client.config  # type: ignore[attr-defined]
        if interaction.user.id != config.log_dm_user_id:
            raise NotOperator("Only the bot operator can run this command.")
        return True

    return app_commands.check(predicate)
