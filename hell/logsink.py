"""Live log stream to a Discord DM.

Everything the bot logs — errors and tracebacks, people joining and leaving the
VC, `@clanker` kicks, alive checks, grace-period warnings, milestones, restarts
— is mirrored in real time to one operator's direct messages.

Design constraints that matter here:

* **Never break the bot.**  The handler only appends to an in-memory deque; a
  background task does the network I/O.  Every failure path is swallowed.
* **Never recurse.**  Records produced by this module (or by discord.py's HTTP
  layer while we are posting) are dropped, otherwise a failing DM would log an
  error, which would try to DM, which would fail…
* **Never get rate limited.**  Lines are batched and flushed at most once every
  `LOG_DM_FLUSH_SECONDS` (default 3 s), at most a couple of messages per flush,
  each inside a 2000-character code block.  Overflow is reported as
  `… N lines dropped` instead of spamming.
* **Never spin forever on a closed inbox.**  If the operator has DMs closed the
  stream disables itself and says so in the file log.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

import discord

from .config import Config
from .tasks import spawn

log = logging.getLogger("hell.logsink")

MAX_MESSAGE = 1900          # margin under Discord's 2000
MAX_MESSAGES_PER_FLUSH = 3  # backpressure: never burst more than this
MAX_LINE = 500              # a single log line is truncated to this

LEVEL_ICON = {
    logging.CRITICAL: "💥",
    logging.ERROR: "❌",
    logging.WARNING: "⚠️",
    logging.INFO: "•",
    logging.DEBUG: "·",
}

# Records from these loggers are never mirrored (they would feed back into the
# stream while we are posting it).
EXCLUDED_PREFIXES = ("hell.logsink", "discord.http", "discord.gateway", "discord.client")


class DiscordLogHandler(logging.Handler):
    """Buffers formatted log lines for the DM stream.  Thread-safe and non-blocking."""

    def __init__(self, capacity: int = 2000, history: int = 200):
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=capacity)
        # `buffer` is drained when the DM is sent; `recent` is a rolling window
        # kept for `/hell logs tail`, so the log is readable even with DMs off.
        self.recent: deque[str] = deque(maxlen=history)
        self.dropped = 0
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if not value:
            self.buffer.clear()   # `recent` survives: tailing still works

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(EXCLUDED_PREFIXES):
            return
        try:
            line = self.render(record)
        except Exception:  # pragma: no cover - formatting must never raise
            return
        self.recent.append(line)
        if not self._enabled:
            return
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1
        self.buffer.append(line)

    def tail(self, limit: int = 20) -> list[str]:
        """The most recent lines, without consuming them."""
        return list(self.recent)[-max(1, limit):]

    def render(self, record: logging.LogRecord) -> str:
        icon = LEVEL_ICON.get(record.levelno, "•")
        stamp = self.format_time(record)
        source = record.name.replace("hell.", "")
        message = record.getMessage()
        if record.exc_info:
            exc = logging.Formatter().formatException(record.exc_info)
            message = f"{message}\n{exc}"
        line = f"{stamp} {icon} [{source}] {message}"
        if len(line) > MAX_LINE:
            line = line[: MAX_LINE - 1] + "…"
        return line

    _time_formatter = logging.Formatter(datefmt="%H:%M:%S")

    @classmethod
    def format_time(cls, record: logging.LogRecord) -> str:
        return cls._time_formatter.formatTime(record, "%H:%M:%S")

    def drain(self) -> tuple[list[str], int]:
        """Take everything buffered so far plus the dropped-line count."""
        lines = list(self.buffer)
        self.buffer.clear()
        dropped, self.dropped = self.dropped, 0
        return lines, dropped


class DiscordLogStream:
    """Owns the handler and the background task that ships lines to the DM."""

    def __init__(self, bot: discord.Client, config: Config):
        self.bot = bot
        self.config = config
        self.handler = DiscordLogHandler()
        self.handler.setLevel(self._level(config.log_dm_level))
        self._task: Optional[asyncio.Task] = None
        self._user: Optional[discord.User] = None
        self._sent = 0
        self._failures = 0
        self.disabled_reason: Optional[str] = None

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _level(name: str) -> int:
        return getattr(logging, str(name).upper(), logging.INFO)

    @property
    def target_id(self) -> int:
        return self.config.log_dm_user_id

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def enabled(self) -> bool:
        return self.handler.enabled and self.disabled_reason is None

    def attach(self) -> None:
        """Start capturing records immediately (before Discord is even up)."""
        root = logging.getLogger()
        if self.handler not in root.handlers:
            root.addHandler(self.handler)
        self._widen_root_level()

    def _widen_root_level(self) -> None:
        """Make sure the root logger passes everything this stream wants.

        The file and console handlers keep their own levels, so lowering the
        root level here only affects what the DM stream can see.
        """
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > self.handler.level:
            root.setLevel(self.handler.level)

    def detach(self) -> None:
        logging.getLogger().removeHandler(self.handler)

    def set_level(self, name: str) -> None:
        self.handler.setLevel(self._level(name))
        self._widen_root_level()

    def set_enabled(self, value: bool) -> None:
        self.handler.set_enabled(value)
        if value:
            self.disabled_reason = None

    def tail(self, limit: int = 20) -> list[str]:
        return self.handler.tail(limit)

    def status(self) -> str:
        if self.disabled_reason:
            return f"disabled — {self.disabled_reason}"
        if not self.handler.enabled:
            return "off"
        level = logging.getLevelName(self.handler.level)
        return f"on ({level}) → <@{self.target_id}> • {self._sent} message(s) sent"

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Resolve the operator and begin streaming."""
        if not self.config.log_dm_enabled:
            self.disabled_reason = "LOG_DM_ENABLED=false"
            return
        if self.running:
            return
        try:
            self._user = self.bot.get_user(self.target_id) or await self.bot.fetch_user(self.target_id)
        except discord.HTTPException as exc:
            self.disabled_reason = f"could not resolve user {self.target_id}: {exc}"
            log.warning("Live log stream disabled — %s", self.disabled_reason)
            return

        self._task = spawn(self._run(), name="log-stream")
        await self._send(
            f"📡 **Live log stream connected** — mirroring `{logging.getLevelName(self.handler.level)}`"
            f" and above from **Welcome to Hell**."
        )

    async def stop(self) -> None:
        """Flush whatever is buffered and stop the task."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.flush()
            await self._send("📴 **Live log stream disconnected** — the bot is shutting down.")
        except Exception:  # pragma: no cover - shutdown must never raise
            pass

    async def _run(self) -> None:
        interval = max(1.0, self.config.log_dm_flush_seconds)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.flush()
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception:  # pragma: no cover - keep the bot alive no matter what
            log.exception("Live log stream crashed")

    # -------------------------------------------------------------- sending

    async def flush(self) -> int:
        """Send everything buffered.  Returns the number of messages posted."""
        if not self.enabled or self._user is None:
            return 0
        lines, dropped = self.handler.drain()
        if dropped:
            lines.append(f"… {dropped} line(s) dropped (log flood)")
        if not lines:
            return 0

        posted = 0
        chunks = self.chunks(lines)
        for index, chunk in enumerate(chunks):
            if posted >= MAX_MESSAGES_PER_FLUSH:
                # Count the *lines* we are dropping, not the chunks.
                suppressed = sum(c.count("\n") + 1 for c in chunks[index:])
                await self._send(f"… {suppressed} more line(s) suppressed to avoid rate limits")
                break
            if await self._send(f"```ansi\n{chunk}\n```"):
                posted += 1
        return posted

    @staticmethod
    def chunks(lines: list[str], limit: int = MAX_MESSAGE) -> list[str]:
        """Pack lines into code-block-sized chunks."""
        out: list[str] = []
        buf: list[str] = []
        size = 0
        for line in lines:
            if len(line) + 1 > limit:  # pathological single line
                line = line[: limit - 2] + "…"
            if size + len(line) + 1 > limit and buf:
                out.append("\n".join(buf))
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            out.append("\n".join(buf))
        return out

    async def _send(self, content: str) -> bool:
        if self._user is None:
            return False
        try:
            await self._user.send(
                content[:2000], allowed_mentions=discord.AllowedMentions.none()
            )
            self._sent += 1
            self._failures = 0
            return True
        except discord.Forbidden:
            self.disabled_reason = "the operator has DMs closed"
            self.handler.set_enabled(False)
            log.warning(
                "Live log stream disabled — user %s does not accept DMs from this bot",
                self.target_id,
            )
            return False
        except discord.HTTPException as exc:
            self._failures += 1
            if self._failures >= 5:
                self.disabled_reason = f"too many delivery failures ({exc})"
                self.handler.set_enabled(False)
                log.warning("Live log stream disabled after repeated failures: %s", exc)
            return False
