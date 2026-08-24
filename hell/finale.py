"""The 160-Hour Finale subsystem.

Governs the final hour (159:00:00 → 160:00:00) of the challenge:
- 159:00:00 (1h left): Activates FINAL_HOUR state & announcement.
- 159:30:00 (30m left): ⚠️ 30 MINUTES REMAIN announcement (no @everyone).
- 159:50:00 (10m left): 🚨 10 MINUTES REMAIN announcement.
- 159:55:00 (5m left): 🔥 5 MINUTES REMAIN announcement.
- 159:59:00 (Final minute): Switches progress display into 1-second FINAL COUNTDOWN mode (60...1).
- 160:00:00 (Exact completion): Atomic completion of the 160h challenge.

All core survival rules (VC monitoring, empty-VC grace, alive checks, dead checks,
difficulty, gambling, Hell Events) remain active throughout the Finale.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .config import Config
from .models import ParticipantRef
from .storage import Store
from .texts import TEXT

log = logging.getLogger("hell.finale")

TOTAL_SECONDS = 160 * 3600.0          # 576,000s
FINAL_HOUR_SECONDS = 159 * 3600.0     # 572,400s (3,600s left)
STAGE_30M_SECONDS = 159.5 * 3600.0    # 574,200s (1,800s left)
STAGE_10M_SECONDS = (159 * 3600.0) + (50 * 60.0)  # 575,400s (600s left)
STAGE_5M_SECONDS = (159 * 3600.0) + (55 * 60.0)   # 575,700s (300s left)
COUNTDOWN_SECONDS = (159 * 3600.0) + (59 * 60.0)  # 575,940s (60s left)


class FinaleStage(str, Enum):
    FINAL_HOUR = "final_hour"
    REMAIN_30M = "remain_30m"
    REMAIN_10M = "remain_10m"
    REMAIN_5M = "remain_5m"
    COUNTDOWN = "countdown"


@dataclass(frozen=True)
class FinaleAnnouncement:
    stage: FinaleStage
    title: str
    description: str
    ping_everyone: bool = False


class FinaleManager:
    """Tracks and dispatches the 160-Hour Finale milestones and countdown."""

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        engine: Optional[Any] = None,
        announcer: Optional[Any] = None,
    ):
        self.config = config
        self.store = store
        self.engine = engine
        self.announcer = announcer
        self._event_uid: Optional[str] = None

    def bind(self, event_uid: Optional[str]) -> None:
        self._event_uid = event_uid

    def reset(self) -> None:
        self._event_uid = None

    @property
    def bound_uid(self) -> Optional[str]:
        return self._event_uid

    # ----------------------------------------------------------- state readers

    def is_final_hour(self, elapsed: float) -> bool:
        """Whether the event has reached or passed 159:00:00."""
        return elapsed >= FINAL_HOUR_SECONDS and elapsed < TOTAL_SECONDS

    def is_countdown(self, elapsed: float) -> bool:
        """Whether the event is in the final 60 seconds (159:59:00 to 160:00:00)."""
        return elapsed >= COUNTDOWN_SECONDS and elapsed < TOTAL_SECONDS

    def countdown_seconds_left(self, elapsed: float) -> Optional[int]:
        """Seconds remaining (60...1) during the final minute."""
        if self.is_countdown(elapsed):
            left = int(TOTAL_SECONDS - elapsed)
            return max(1, min(60, left))
        return None

    def completed_stages(self) -> set[str]:
        if not self._event_uid:
            return set()
        return self.store.get_finale_stages(self._event_uid)

    # ------------------------------------------------------------------ tick

    async def tick(
        self, now: float, elapsed: float, participants: Sequence[ParticipantRef]
    ) -> list[FinaleAnnouncement]:
        """Check if any finale announcements are due at the current elapsed time."""
        if not self._event_uid:
            return []
        if self.engine is not None and (not self.engine.is_running or self.engine.is_paused):
            return []

        stages = self.completed_stages()
        announcements: list[FinaleAnnouncement] = []

        # 1) Final Hour (159:00:00 / 1h left)
        if elapsed >= FINAL_HOUR_SECONDS and FinaleStage.FINAL_HOUR.value not in stages:
            self.store.mark_finale_stage(self._event_uid, FinaleStage.FINAL_HOUR.value)
            ann = FinaleAnnouncement(
                stage=FinaleStage.FINAL_HOUR,
                title=str(getattr(TEXT, "FINALE_FINAL_HOUR_TITLE", "👹 THE FINAL HOUR")),
                description=str(
                    getattr(
                        TEXT,
                        "FINALE_FINAL_HOUR_ANNOUNCE",
                        "👹 **THE FINAL HOUR HAS BEGUN**\n\n**1 HOUR REMAINING**\nDO NOT LET HELL GO EMPTY.",
                    )
                ),
                ping_everyone=False,
            )
            announcements.append(ann)
            log.info("Entered Finale Stage: FINAL_HOUR (159h)")

        # 2) 30 Minutes Remain (159:30:00)
        if elapsed >= STAGE_30M_SECONDS and FinaleStage.REMAIN_30M.value not in stages:
            self.store.mark_finale_stage(self._event_uid, FinaleStage.REMAIN_30M.value)
            ann = FinaleAnnouncement(
                stage=FinaleStage.REMAIN_30M,
                title=str(getattr(TEXT, "FINALE_30M_TITLE", "⚠️ 30 MINUTES REMAIN")),
                description=str(
                    getattr(
                        TEXT,
                        "FINALE_30M_ANNOUNCE",
                        "⚠️ **30 MINUTES REMAIN**\n\nHell is crumbling. Keep the voice channel alive!",
                    )
                ),
                ping_everyone=False,
            )
            announcements.append(ann)
            log.info("Entered Finale Stage: 30 MINUTES REMAIN")

        # 3) 10 Minutes Remain (159:50:00)
        if elapsed >= STAGE_10M_SECONDS and FinaleStage.REMAIN_10M.value not in stages:
            self.store.mark_finale_stage(self._event_uid, FinaleStage.REMAIN_10M.value)
            ann = FinaleAnnouncement(
                stage=FinaleStage.REMAIN_10M,
                title=str(getattr(TEXT, "FINALE_10M_TITLE", "🚨 10 MINUTES REMAIN")),
                description=str(
                    getattr(
                        TEXT,
                        "FINALE_10M_ANNOUNCE",
                        "🚨 **10 MINUTES REMAIN**\n\n**HELL IS ALMOST CONQUERED.**",
                    )
                ),
                ping_everyone=False,
            )
            announcements.append(ann)
            log.info("Entered Finale Stage: 10 MINUTES REMAIN")

        # 4) 5 Minutes Remain (159:55:00)
        if elapsed >= STAGE_5M_SECONDS and FinaleStage.REMAIN_5M.value not in stages:
            self.store.mark_finale_stage(self._event_uid, FinaleStage.REMAIN_5M.value)
            ann = FinaleAnnouncement(
                stage=FinaleStage.REMAIN_5M,
                title=str(getattr(TEXT, "FINALE_5M_TITLE", "🔥 5 MINUTES REMAIN")),
                description=str(
                    getattr(
                        TEXT,
                        "FINALE_5M_ANNOUNCE",
                        "🔥 **5 MINUTES REMAIN**\n\nThe end of Hell is in sight. Stay in the VC!",
                    )
                ),
                ping_everyone=False,
            )
            announcements.append(ann)
            log.info("Entered Finale Stage: 5 MINUTES REMAIN")

        # Broadcast any new finale announcements
        if self.announcer is not None:
            for ann in announcements:
                try:
                    await self.announcer.announce_finale_stage(ann)
                except Exception:
                    log.exception("Failed to broadcast finale stage announcement %s", ann.stage.value)

        return announcements
