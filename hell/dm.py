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

    def reports(self) -> list[UserReport]:
        """One report per contestant for the current (finished) event."""
        return build_reports(
            self.engine.leaderboard(),
            self.engine.milestone_records(),
            status=self.engine.status,
            event_elapsed=self.engine.elapsed(),
        )

    def report_for(self, user_id: int) -> Optional[UserReport]:
        for report in self.reports():
            if report.user_id == user_id:
                return report
        return None

    def build_embed(self, report: UserReport) -> discord.Embed:
        embed = discord.Embed(
            title=TEXT.CARD_EMBED_TITLE,
            description=render_report(report),
            color=int(TEXT.COLOR_CARD),
        )
        embed.set_footer(text=TEXT.CARD_EMBED_FOOTER)
        return embed

    # -------------------------------------------------------------- sending

    def pending(self) -> list[UserReport]:
        """Contestants who have not been messaged yet for this event."""
        uid = self.engine.event_uid
        if not uid:
            return []
        done = self.engine.store.dm_recipients(uid)
        return [r for r in self.reports() if r.user_id not in done]

    def schedule(self) -> None:
        """Kick off delivery in the background (safe to call more than once)."""
        if not self.config.send_final_dms:
            log.info("Final DMs disabled (SEND_FINAL_DMS=false)")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = spawn(self.send_all(), name="stat-cards")

    async def send_all(self) -> dict[str, int]:
        """Send every pending stat card.  Returns a small summary."""
        summary = {"sent": 0, "blocked": 0, "failed": 0, "total": 0}
        if not self.config.send_final_dms:
            return summary

        async with self._lock:
            uid = self.engine.event_uid
            if not uid or not self.engine.status.is_terminal:
                return summary
            pending = self.pending()
            summary["total"] = len(pending)
            if not pending:
                return summary

            log.info("Sending %d end-of-event stat card(s)", len(pending))
            for report in pending:
                try:
                    status = await self._send_one(report)
                    self.engine.store.record_dm(uid, report.user_id, status)
                except Exception:
                    log.exception("Could not deliver the stat card for %s", report.user_id)
                    status = "failed"
                summary[status] = summary.get(status, 0) + 1
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
