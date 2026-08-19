"""Bot supervisor used by the desktop launcher.

Runs the Discord bot on its own asyncio loop in a background thread so the GUI
stays responsive, exposes a thread-safe log stream, and reports live event
state for the dashboard.  Contains no GUI code, so it is unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from hell.config import Config, ConfigError
from hell.logging_setup import setup_logging
from hell.milestones import TOTAL_SECONDS
from hell.models import EventStatus
from hell.paths import env_path, log_file
from hell.timeutil import format_hm

from . import envfile

log = logging.getLogger("hell.launcher")

STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
ERROR = "ERROR"


class QueueLogHandler(logging.Handler):
    """Ships formatted log records to the GUI through a bounded queue."""

    def __init__(self, maxsize: int = 5000):
        super().__init__()
        self.queue: "queue.Queue[str]" = queue.Queue(maxsize=maxsize)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - formatting must never crash the bot
            return
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            try:  # drop the oldest line and keep the newest
                self.queue.get_nowait()
                self.queue.put_nowait(message)
            except queue.Empty:  # pragma: no cover
                pass

    def drain(self, limit: int = 500) -> list[str]:
        out: list[str] = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out


@dataclass
class Stats:
    """Everything the dashboard shows."""

    status: str = STOPPED
    detail: str = ""
    uptime: float = 0.0
    connected_as: str = ""
    event_status: str = EventStatus.IDLE.value
    elapsed: float = 0.0
    total: float = TOTAL_SECONDS
    fraction: float = 0.0
    remaining: float = TOTAL_SECONDS
    participants: int = 0
    next_milestone: str = "—"
    time_to_next: str = "—"
    health_errors: list[str] = field(default_factory=list)
    health_warnings: list[str] = field(default_factory=list)

    @property
    def elapsed_text(self) -> str:
        return f"{format_hm(self.elapsed)} / {format_hm(self.total)}"

    @property
    def percent_text(self) -> str:
        return f"{self.fraction * 100:.1f}%"


class BotSupervisor:
    """Start/stop the bot in a worker thread and expose its state."""

    def __init__(
        self,
        env_file: Optional[Path] = None,
        *,
        bot_factory: Optional[Callable[[Config], Any]] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
    ):
        self.env_file = Path(env_file) if env_file else env_path()
        self._bot_factory = bot_factory
        self._on_status_change = on_status_change

        self.log_handler = QueueLogHandler()
        self.log_path = setup_logging("INFO", extra_handler=self.log_handler)

        self._status = STOPPED
        self._detail = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot: Any = None
        self._started_at: Optional[float] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------- status

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status in (STARTING, RUNNING, STOPPING)

    def _set_status(self, status: str, detail: str = "") -> None:
        with self._lock:
            self._status = status
            self._detail = detail
        log.debug("Supervisor status -> %s (%s)", status, detail)
        if self._on_status_change:
            try:
                self._on_status_change(status, detail)
            except Exception:  # pragma: no cover - UI callback must not break us
                log.exception("status callback failed")

    # ------------------------------------------------------------ config

    def load_config(self) -> Config:
        """Build a Config from the launcher's `.env` (raises ConfigError)."""
        values = envfile.read_env(self.env_file)
        problems = envfile.validate(values)
        if problems:
            raise ConfigError("\n".join(problems))
        envfile.apply_to_environ(values, os.environ)
        return Config.from_env(env_file=self.env_file)

    # ------------------------------------------------------- start / stop

    def start(self) -> None:
        """Launch the bot thread.  Raises ConfigError on a bad configuration."""
        if self.is_running:
            raise RuntimeError("The bot is already running.")

        config = self.load_config()  # validate before spawning anything
        setup_logging(config.log_level, extra_handler=None, force=False)
        logging.getLogger().setLevel(getattr(logging, config.log_level, logging.INFO))

        self._set_status(STARTING, "connecting to Discord…")
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._thread_main, args=(config,), name="hellbot", daemon=True)
        self._thread.start()

    def _thread_main(self, config: Config) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        bot = None
        try:
            if self._bot_factory is not None:
                bot = self._bot_factory(config)
            else:
                from hell.bot import build_bot  # imported lazily: keeps tests light

                bot = build_bot(config)
            self._bot = bot
            self._set_status(RUNNING, "connected")
            loop.run_until_complete(bot.start(config.token))
            self._set_status(STOPPED, "stopped")
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            message = self._humanise(exc)
            log.error("Bot stopped: %s", message)
            self._set_status(ERROR, message)
        finally:
            try:
                if bot is not None and not getattr(bot, "is_closed", lambda: True)():
                    loop.run_until_complete(bot.close())
            except Exception:  # pragma: no cover
                log.debug("close() during shutdown failed", exc_info=True)
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover
                pass
            loop.close()
            self._loop = None
            self._bot = None
            self._started_at = None
            if self._status not in (ERROR,):
                self._set_status(STOPPED, "stopped")

    @staticmethod
    def _humanise(exc: BaseException) -> str:
        name = type(exc).__name__
        text = str(exc) or name
        if "LoginFailure" in name or "Improper token" in text:
            return "Discord rejected the token. Check DISCORD_TOKEN in Settings."
        if "PrivilegedIntentsRequired" in name:
            return (
                "The Server Members intent is disabled. Enable it in the Discord Developer "
                "Portal (Bot -> Privileged Gateway Intents) and start again."
            )
        if "ClientConnectorError" in name or "Cannot connect" in text:
            return "Could not reach Discord. Check the internet connection and try again."
        return f"{name}: {text}"

    def stop(self, timeout: float = 20.0) -> None:
        """Ask the bot to close and wait for the thread to finish."""
        if not self.is_running:
            return
        self._set_status(STOPPING, "closing the Discord connection…")
        loop, bot = self._loop, self._bot
        if loop is not None and bot is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
                future.result(timeout=timeout / 2)
            except Exception:  # pragma: no cover - best effort
                log.debug("close() did not complete cleanly", exc_info=True)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover
                log.warning("Bot thread did not stop within %.0fs", timeout)
        self._thread = None
        if self._status != ERROR:
            self._set_status(STOPPED, "stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    # -------------------------------------------------------------- stats

    def stats(self) -> Stats:
        """A snapshot for the dashboard; safe to call from the UI thread."""
        stats = Stats(status=self._status, detail=self._detail)
        if self._started_at:
            stats.uptime = time.time() - self._started_at

        bot = self._bot
        if bot is None:
            summary = self._offline_summary()
            if summary:
                stats.event_status = summary["status"]
                stats.elapsed = summary["elapsed"]
                stats.fraction = summary["fraction"]
                stats.remaining = summary["remaining"]
            return stats

        try:
            user = getattr(bot, "user", None)
            stats.connected_as = str(user) if user else ""
            engine = getattr(bot, "engine", None)
            if engine is not None:
                snap = engine.snapshot()
                stats.event_status = snap.status.value
                stats.elapsed = snap.elapsed
                stats.total = snap.total
                stats.fraction = snap.fraction
                stats.remaining = snap.remaining
                stats.participants = snap.participants
                stats.next_milestone = f"{snap.upcoming.hours}h" if snap.upcoming else "—"
                stats.time_to_next = format_hm(snap.time_to_next) if snap.time_to_next else "—"
            health = getattr(bot, "health", None)
            if health is not None:
                stats.health_errors = list(health.errors)
                stats.health_warnings = list(health.warnings)
        except Exception:  # pragma: no cover - stats must never raise into the UI
            log.debug("stats() failed", exc_info=True)
        return stats

    def _offline_summary(self) -> Optional[dict]:
        """Read the last saved event state without starting the bot."""
        try:
            values = envfile.read_env(self.env_file)
            raw = str(values.get("DATABASE_PATH", "") or "data/hell.sqlite3")
            from hell.paths import resolve
            from hell.storage import Store

            path = resolve(raw)
            if not Path(path).exists():
                return None
            store = Store(path)
            try:
                state = store.load_state()
                if state.start_ts is None:
                    return {"status": state.status.value, "elapsed": 0.0, "fraction": 0.0,
                            "remaining": TOTAL_SECONDS}
                reference = state.end_ts if (state.status.is_terminal and state.end_ts) else time.time()
                elapsed = max(0.0, min(reference, state.start_ts + TOTAL_SECONDS) - state.start_ts)
                return {
                    "status": state.status.value,
                    "elapsed": elapsed,
                    "fraction": elapsed / TOTAL_SECONDS,
                    "remaining": max(0.0, TOTAL_SECONDS - elapsed),
                }
            finally:
                store.close()
        except Exception:  # pragma: no cover
            log.debug("offline summary failed", exc_info=True)
            return None

    # -------------------------------------------------------------- misc

    def log_file_path(self) -> Path:
        return Path(self.log_path or log_file())
