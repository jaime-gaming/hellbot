"""The end-of-160h "keep on Hell?" vote.

After a run reaches 160h COMPLETED and the personal stat cards have been sent,
the bot posts one announcement in the announcement channel:

* "📬 Something has been sent to your DM."
* an embed with two buttons: **Yes, keep Hell going** and **No, let it end**.

The vote stays open for ``CONTINUATION_VOTE_SECONDS`` (10 minutes).  Once the
window closes the most-voted answer is published.  If **Yes** won, a host can
run ``/hell resume`` to continue the same run into **Hell 2**:

* total clock extends from 160h to **320h**;
* there are **no milestones** after 160h;
* at 320h there is only a final, secret reward.

The poll is stored in SQLite so a restart in the middle of the window does not
lose votes, and the closing timer is re-armed on startup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import InteractionType

from .config import Config
from .engine import HellEngine
from .models import EventStatus
from .tasks import spawn
from .texts import TEXT, say
from .timeutil import now_ts

log = logging.getLogger("hell.continuation")

CONTINUATION_VOTE_SECONDS = 10 * 60.0   # 10 minutes
YES_CUSTOM_ID = "hell_continue_yes_v1"
NO_CUSTOM_ID = "hell_continue_no_v1"


class ContinuationPollView(discord.ui.View):
    """Yes/No buttons for the 160h continuation vote."""

    def __init__(self, manager: ContinuationManager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="🔥 Yes, keep Hell going",
        style=discord.ButtonStyle.success,
        custom_id=YES_CUSTOM_ID,
        emoji="🔥",
    )
    async def yes_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.manager.vote(interaction, "yes")

    @discord.ui.button(
        label="🛑 No, let it end",
        style=discord.ButtonStyle.danger,
        custom_id=NO_CUSTOM_ID,
        emoji="🛑",
    )
    async def no_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.manager.vote(interaction, "no")


class ContinuationManager:
    """Owns the single 160h continuation poll and the 320h resume gate."""

    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        engine: HellEngine,
        announcer=None,
    ):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.announcer = announcer
        self._timer_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- helpers

    def view(self) -> ContinuationPollView:
        return ContinuationPollView(self)

    def _poll(self) -> Optional[dict]:
        return self.engine.store.get_continuation_poll()

    def _active(self) -> bool:
        poll = self._poll()
        return bool(poll and poll.get("status") == "open")

    def yes_won(self) -> bool:
        """Whether a closed continuation poll has a Yes majority."""
        poll = self._poll()
        if not poll:
            return False
        poll_uid = poll.get("event_uid")
        if not poll_uid or poll.get("status") not in ("closed", "resumed"):
            return False
        counts = self.engine.store.continuation_vote_counts(poll_uid)
        return counts.get("yes", 0) > counts.get("no", 0)

    # -------------------------------------------------------------- lifecycle

    async def ensure_for(self, event_uid: Optional[str]) -> bool:
        """Make sure a poll exists for a COMPLETED 160h run (idempotent).

        Returns True when the poll was created or already open.
        """
        if not event_uid:
            return False
        if self.engine.status is not EventStatus.COMPLETED or self.engine.is_continuation:
            return False
        poll = self._poll()
        if poll is not None and poll.get("event_uid") == event_uid:
            status = poll.get("status")
            if status == "open":
                self._arm_timer(poll)
                if self._expired(poll):
                    await self.close()
                return True
            if status in ("closed", "resumed"):
                return False
        # No poll yet for this completed run — open one.
        return await self.start(event_uid)

    async def start(self, event_uid: str) -> bool:
        """Create the poll message and start the 10-minute countdown."""
        if self.engine.status is not EventStatus.COMPLETED or self.engine.is_continuation:
            return False
        if self._poll() is not None and self._poll().get("event_uid") == event_uid:
            # Already exists; just make sure it is live.
            return await self.ensure_for(event_uid)

        deadline = now_ts() + CONTINUATION_VOTE_SECONDS
        embed = self._question_embed()
        chan = await self._announcement_channel()
        if chan is None:
            self.engine.store.set_continuation_poll(
                event_uid, channel_id=None, message_id=None,
                started_ts=now_ts(), deadline_ts=deadline, status="open",
            )
            log.warning("Continuation vote could not be posted (channel unreachable)")
            self._arm_timer({"event_uid": event_uid, "deadline_ts": deadline, "status": "open"})
            return False

        try:
            message = await chan.send(embed=embed, view=self.view())
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not post continuation vote: %s", exc)
            return False
        self.engine.store.set_continuation_poll(
            event_uid,
            channel_id=message.channel.id,
            message_id=message.id,
            started_ts=now_ts(),
            deadline_ts=deadline,
            status="open",
        )
        log.info("Continuation vote opened for %s (closes in %.0f min)", event_uid,
                 CONTINUATION_VOTE_SECONDS / 60)
        self._arm_timer({"event_uid": event_uid, "deadline_ts": deadline, "status": "open"})
        return True

    async def close(self) -> dict[str, int]:
        """Close the poll if it is still open, publish the result."""
        poll = self._poll()
        if not poll or poll.get("status") not in ("open",):
            return {}
        event_uid = poll.get("event_uid") or ""
        counts = self.engine.store.continuation_vote_counts(event_uid)
        result = "yes" if counts.get("yes", 0) > counts.get("no", 0) else "no"
        self.engine.store.set_continuation_poll(
            event_uid,
            channel_id=poll.get("channel_id"),
            message_id=poll.get("message_id"),
            started_ts=poll.get("started_ts"),
            deadline_ts=poll.get("deadline_ts"),
            status="closed",
            result=result,
        )
        await self._update_poll_message(result, counts)
        log.info("Continuation vote closed for %s: yes=%d no=%d -> %s",
                 event_uid, counts.get("yes", 0), counts.get("no", 0), result)
        return counts

    # ----------------------------------------------------------------- votes

    async def vote(self, interaction: discord.Interaction, answer: str) -> None:
        poll = self._poll()
        if not poll or poll.get("status") != "open":
            await self._reply(interaction, TEXT.CONTINUATION_VOTE_CLOSED)
            return
        event_uid = poll.get("event_uid") or ""
        self.engine.store.record_continuation_vote(
            event_uid, interaction.user.id, answer, now_ts()
        )
        await self._reply(
            interaction,
            say(
                TEXT.CONTINUATION_VOTE_RECORDED,
                answer="Yes, keep Hell going" if answer == "yes" else "No, let it end",
            ),
        )

    # ------------------------------------------------------------------ timer

    def _arm_timer(self, poll: Optional[dict]) -> None:
        """Cancel the previous timer and schedule the close at the deadline."""
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        if not poll:
            return
        deadline = float(poll.get("deadline_ts") or 0)
        if deadline <= now_ts():
            return
        self._timer_task = spawn(self._wait_and_close(deadline), name="continuation-vote")

    async def _wait_and_close(self, deadline: float) -> None:
        wait = max(0.0, deadline - now_ts())
        await asyncio.sleep(wait)
        try:
            await self.close()
        except Exception:  # pragma: no cover - never break the loop
            log.exception("Could not close the continuation vote")

    def _expired(self, poll: dict) -> bool:
        return float(poll.get("deadline_ts") or 0) <= now_ts()

    # ------------------------------------------------------------------ posts

    def _question_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=str(getattr(TEXT, "CONTINUATION_TITLE", "📬 Something has been sent to your DM")),
            description=say(
                getattr(
                    TEXT,
                    "CONTINUATION_DESCRIPTION",
                    "**Will you like to keep on Hell or not?**\n\n"
                    "Your personal stat card has been sent to your DMs. "
                    "If most people vote **Yes**, the hosts can continue the same run to **320h**.",
                ),
                seconds=int(CONTINUATION_VOTE_SECONDS // 60),
            ),
            color=int(getattr(TEXT, "COLOR_CONTINUATION", int(getattr(TEXT, "COLOR_RUNNING", 0xE25822)))),
        )
        embed.set_footer(text=(
            f"Voting closes in {int(CONTINUATION_VOTE_SECONDS // 60)} minutes · one vote per person"
        ))
        return embed

    async def _update_poll_message(self, result: str, counts: dict[str, int]) -> None:
        poll = self._poll()
        if not poll or not poll.get("channel_id") or not poll.get("message_id"):
            return
        channel = self.bot.get_channel(int(poll["channel_id"]))
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return
        try:
            message = await channel.fetch_message(int(poll["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        embed = discord.Embed(
            title=str(getattr(TEXT, "CONTINUATION_RESULT_TITLE", "🗳️ The vote has closed")),
            description=say(
                getattr(
                    TEXT,
                    "CONTINUATION_RESULT_DESCRIPTION",
                    "**Yes**: {yes} vote(s)  ·  **No**: {no} vote(s)",
                ),
                yes=counts.get("yes", 0),
                no=counts.get("no", 0),
            ),
            color=int(getattr(TEXT, "COLOR_CONTINUATION", int(getattr(TEXT, "COLOR_RUNNING", 0xE25822)))),
        )
        if result == "yes":
            embed.add_field(
                name=getattr(TEXT, "CONTINUATION_RESULT_YES_FIELD", "🔥 The run can continue"),
                value=str(
                    getattr(
                        TEXT,
                        "CONTINUATION_RESULT_YES_TEXT",
                        "Most people voted **Yes**. A host can now run `/hell resume` to keep the same run going to **320h**.",
                    )
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name=getattr(TEXT, "CONTINUATION_RESULT_NO_FIELD", "🛑 Hell has ended"),
                value=str(getattr(TEXT, "CONTINUATION_RESULT_NO_TEXT", "The majority said **No** — Hell stays conquered.")),
                inline=False,
            )
        try:
            await message.edit(embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not update closed continuation vote: %s", exc)

    # --------------------------------------------------------------- channels

    async def _announcement_channel(self) -> Optional[discord.abc.Messageable]:
        if self.announcer is None:
            return None
        return await self.announcer.channel()

    async def _reply(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.type is InteractionType.component:
            try:
                await interaction.response.send_message(text, ephemeral=True)
                return
            except discord.HTTPException:
                pass
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            pass
