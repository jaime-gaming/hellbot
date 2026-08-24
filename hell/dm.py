"""Delivery of the end-of-event stat cards by direct message.

When a run ends (COMPLETED, FAILED or CANCELLED) every contestant on the
frozen leaderboard receives a DM with their own numbers.  Delivery is:

* **resumable** — every attempt is written to `dm_log`, so a restart halfway
  through continues where it stopped instead of double-messaging people;
* **rate-limit friendly** — one DM per `DM_DELAY_SECONDS` (default 1s), run in
  a background task so the 1-second VC monitor is never blocked;
* **failure tolerant** — users with DMs closed are recorded as `blocked` and
  reported in the channel summary; they can still use `/hell mystats`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from .config import Config
from .engine import HellEngine
from .models import EventStatus
from .reports import UserReport, build_reports, render_report
from .tasks import spawn
from .texts import TEXT, say

log = logging.getLogger("hell.dm")


class FinalReportDM:
    """Builds and sends the per-user stat cards."""

    def __init__(self, bot: discord.Client, config: Config, engine: HellEngine, announcer=None):
        self.bot = bot
        self.config = config
        self.engine = engine
        self.announcer = announcer
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- building

    def reports(
        self,
        uid: Optional[str] = None,
        *,
        status: Optional[EventStatus] = None,
        event_elapsed: Optional[float] = None,
    ) -> list[UserReport]:
        """One report per contestant for the given (finished) event.

        `uid` defaults to the current event.  Passing an explicit UID (plus
        the captured status/elapsed) lets delivery stay pinned to the event it
        belongs to even if a new event is started while the cards are going
        out — otherwise the cards could be built from a different event's
        leaderboard entirely.
        """
        uid = uid or self.engine.event_uid
        return build_reports(
            self.engine.leaderboard(uid),
            self.engine.milestone_records(uid),
            status=status if status is not None else self.engine.status,
            event_elapsed=event_elapsed if event_elapsed is not None else self.engine.elapsed(),
        )

    def report_for(self, user_id: int) -> Optional[UserReport]:
        for report in self.reports():
            if report.user_id == user_id:
                return report
        return None

    def build_embed(self, report: UserReport) -> discord.Embed:
        footer = (
            getattr(TEXT, "CARD_EMBED_FOOTER_RUNNING", "Event in progress · Keep surviving to climb the ranks.")
            if report.status is EventStatus.RUNNING
            else getattr(TEXT, "CARD_EMBED_FOOTER", "Thanks for surviving with us. See you in the next one.")
        )
        embed = discord.Embed(
            title=TEXT.CARD_EMBED_TITLE,
            description=render_report(report),
            color=int(TEXT.COLOR_CARD),
        )
        embed.set_footer(text=footer)
        return embed

    # -------------------------------------------------------------- sending

    def pending(
        self,
        uid: Optional[str] = None,
        *,
        status: Optional[EventStatus] = None,
        event_elapsed: Optional[float] = None,
    ) -> list[UserReport]:
        """Contestants who have not been successfully messaged yet for this event.

        'pending' status means the DM was queued but the API call may not have
        completed — those are retried.  Only 'sent', 'blocked' and 'failed'
        are considered terminal.  `uid` defaults to the current event; pass an
        explicit one (with the captured status/elapsed) when delivering for an
        event that may no longer be the current one.
        """
        uid = uid or self.engine.event_uid
        if not uid:
            return []
        done_statuses = self.engine.store.dm_recipients_by_status(uid)
        attempted = done_statuses.keys()
        return [
            r for r in self.reports(uid, status=status, event_elapsed=event_elapsed)
            if r.user_id not in attempted or done_statuses.get(r.user_id) == "pending"
        ]

    def schedule(self) -> None:
        """Kick off delivery in the background (safe to call more than once)."""
        if not self.config.send_final_dms:
            log.info("Final DMs disabled (SEND_FINAL_DMS=false)")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = spawn(self.send_all(), name="stat-cards")

    async def send_all(self) -> dict[str, int]:
        """Send every pending stat card.  Returns a small summary.

        CRASH-SAFE ORDERING: Each DM is recorded as ``pending`` in SQLite
        BEFORE the Discord API call.  If the bot crashes after the DB write
        but before the DM is sent, the ``pending`` status will cause a retry
        on the next start (see ``pending()``).  If the crash happens after the
        send but before the status update, the next start re-checks
        ``dm_recipients_by_status`` and skips users with ``sent`` / ``blocked``
        status — so nobody is double-messaged either.
        """
        summary = {"sent": 0, "blocked": 0, "failed": 0, "total": 0}
        if not self.config.send_final_dms:
            return summary

        async with self._lock:
            uid = self.engine.event_uid
            if not uid or not self.engine.status.is_terminal:
                return summary
            # Capture status/elapsed NOW.  A host can start a new event while
            # these cards are being sent; everything below stays pinned to
            # `uid` so the wrong event's data is never used.
            status = self.engine.status
            event_elapsed = self.engine.elapsed()
            pending = self.pending(uid, status=status, event_elapsed=event_elapsed)
            summary["total"] = len(pending)
            if not pending:
                return summary

            log.info("Sending %d end-of-event stat card(s)", len(pending))
            for report in pending:
                # 1) Mark as "pending" in DB before any Discord I/O.
                self.engine.store.record_dm(uid, report.user_id, "pending")
                try:
                    outcome = await self._send_one(report)
                except Exception:
                    log.exception("Could not deliver the stat card for %s", report.user_id)
                    outcome = "failed"
                # 2) Update the real status after the API call.
                self.engine.store.record_dm(uid, report.user_id, outcome)
                summary[outcome] = summary.get(outcome, 0) + 1
                await asyncio.sleep(max(0.0, self.config.dm_delay_seconds))

            log.info(
                "Stat cards done: %d sent, %d blocked, %d failed",
                summary["sent"],
                summary["blocked"],
                summary["failed"],
            )
            await self._post_summary(summary)
        return summary

    async def _send_one(self, report: UserReport) -> str:
        try:
            user = self.bot.get_user(report.user_id) or await self.bot.fetch_user(report.user_id)
        except discord.HTTPException as exc:
            log.warning("Could not resolve user %s: %s", report.user_id, exc)
            return "failed"
        try:
            await user.send(embed=self.build_embed(report))
            return "sent"
        except discord.Forbidden:
            log.info("User %s has DMs closed — stat card not delivered", report.user_id)
            return "blocked"
        except discord.HTTPException as exc:
            log.warning("Failed to DM %s: %s", report.user_id, exc)
            return "failed"

    async def _post_summary(self, summary: dict[str, int]) -> None:
        if self.announcer is None or not summary["total"]:
            return
        embed = discord.Embed(
            title=TEXT.DM_SUMMARY_TITLE,
            description=say(TEXT.DM_SUMMARY_TEXT, sent=summary["sent"]),
            color=int(TEXT.COLOR_CARD),
        )
        if summary["blocked"]:
            embed.add_field(
                name=TEXT.DM_SUMMARY_BLOCKED_FIELD,
                value=say(TEXT.DM_SUMMARY_BLOCKED_TEXT, blocked=summary["blocked"]),
                inline=False,
            )
        if summary["failed"]:
            embed.add_field(
                name=TEXT.DM_SUMMARY_FAILED_FIELD,
                value=say(TEXT.DM_SUMMARY_FAILED_TEXT, failed=summary["failed"]),
                inline=False,
            )
        await self.announcer.send([embed])
