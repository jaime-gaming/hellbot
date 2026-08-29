"""Host broadcast by DM: one markdown message to every contestant.

Complements the channel broadcast (embed in announcements / VC chat) for the
moments where a DM is the right channel — "maintenance at X", "server move",
anything the host wants on every participant's phone.  Delivery follows the
same rules as the end-of-event stat cards (``hell/dm.py``):

* one DM per ``DM_DELAY_SECONDS`` (default 1s) — well under Discord's
  per-user DM rate limits, since every message goes to a different user;
* users with DMs closed are counted as ``blocked``, not retried;
* a network outage (``UNREACHABLE``) stops the run: retrying against a dead
  connection would only waste the delay budget and report the same failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import discord

from .neterrors import UNREACHABLE

log = logging.getLogger("hell.broadcast")


@dataclass
class BroadcastResult:
    total: int
    delivered: int = 0
    blocked: int = 0            # DMs closed — nothing we can do
    failed: list[int] = field(default_factory=list)  # unknown user / Discord error
    network_down: bool = False  # connection died mid-run: stop, don't fake the rest

    @property
    def ok(self) -> bool:
        """Completed without a hard failure (closed DMs are a count, not a failure)."""
        return not self.network_down and not self.failed


async def dm_participants(
    bot: Any,
    user_ids: Sequence[int],
    text: str,
    *,
    delay: float = 1.0,
) -> BroadcastResult:
    """Send ``text`` (markdown) as a DM to every user in ``user_ids``.

    ``bot.get_user`` / ``bot.fetch_user`` / ``user.send`` are the only
    surfaces used, so the flow is unit-testable with a fake bot.
    """
    result = BroadcastResult(total=len(user_ids))
    for uid in user_ids:
        user = bot.get_user(uid)
        if user is None:
            try:
                user = await bot.fetch_user(uid)
            except discord.HTTPException:
                user = None
        if user is None:
            log.warning("Broadcast DM: user %d could not be resolved", uid)
            result.failed.append(uid)
            continue
        try:
            await user.send(text)
            result.delivered += 1
        except discord.Forbidden:
            # DMs closed — same policy as the final stat cards.
            result.blocked += 1
        except UNREACHABLE as exc:
            # The egress itself is down: every further attempt fails the same
            # way, so stop and report honestly instead of burning the delay.
            log.error("Broadcast DM: network went down mid-run (%s: %s)",
                      type(exc).__name__, exc)
            result.network_down = True
            break
        except discord.HTTPException as exc:
            log.warning("Broadcast DM: send to %d failed: %s", uid, exc)
            result.failed.append(uid)
        if delay > 0:
            await asyncio.sleep(delay)
    return result
