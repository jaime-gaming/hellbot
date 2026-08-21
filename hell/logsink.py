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
* **Alarm bell.**  A line at `LOG_DM_PING_LEVEL` (default ERROR) or worse — or
  any Discord rate limit — additionally triggers a DM that **@-pings** the
  operator, so "something is wrong" is impossible to miss.  Pings are throttled
  to one per `LOG_DM_PING_COOLDOWN_SECONDS` (default 5 min) so an error storm
  cannot spam the operator's phone.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Optional

import discord

from .config import Config
from .tasks import spawn
from .texts import TEXT, say

log = logging.getLogger("hell.logsink")

MAX_MESSAGE = 1900          # margin under Discord's 2000
MAX_MESSAGES_PER_FLUSH = 3  # backpressure: never burst more than this
MAX_LINE = 500              # a single log line is truncated to this
MAX_ALERT_LINES = 10        # an alert ping carries at most this many lines
MIN_PING_COOLDOWN = 30.0    # never ping faster than this, whatever the config says

LEVEL_ICON = {
    logging.CRITICAL: "💥",
    logging.ERROR: "❌",
    logging.WARNING: "⚠️",
    logging.INFO: "•",
    logging.DEBUG: "·",
}

# Records from these loggers are never mirrored — they would feed back into the
# stream while we are posting it.  `discord.http` is deliberately NOT here so
# that rate limits and HTTP failures are visible (and can trigger a ping); its
# records are instead suppressed only while the stream itself is sending (see
# `DiscordLogHandler.sending`).
EXCLUDED_PREFIXES = ("hell.logsink", "discord.gateway", "discord.client")
# While the stream is sending, everything from discord.py's own loggers is
# dropped — otherwise a failed DM (which logs through discord.http) would be
# mirrored and ping forever.
SENDING_GUARD_PREFIXES = ("discord.http", "discord.gateway", "discord.client")

# Rate-limit lines are WARNINGs from discord.http; they are always an alert
# even when LOG_DM_PING_LEVEL is higher, because they are exactly the kind of
# "something is wrong" the operator asked to be pinged about.
RATE_LIMIT_MARKERS = ("rate limit", "429")


class DiscordLogHandler(logging.Handler):
    """Buffers formatted log lines for the DM stream.  Thread-safe and non-blocking."""

    def __init__(self, capacity: int = 2000, history: int = 200):
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=capacity)
        # `buffer` is drained when the DM is sent; `recent` is a rolling window
        # kept for `/hell logs tail`, so the log is readable even with DMs off.
        self.recent: deque[str] = deque(maxlen=history)
        # Lines severe enough to warrant an @-ping (ERROR+ by default).  They
        # are also in `buffer`; this is just the alarm list.
        self.alert_lines: deque[str] = deque(maxlen=MAX_ALERT_LINES)
        self.dropped = 0
        self._enabled = True
        # Minimum severity that counts as an alert (stream sets it from config).
        self.alert_level = logging.ERROR
        # Set by the stream while it is posting DMs, to suppress discord.py's
        # own HTTP/gateway/client records and prevent a feedback loop.
        self.sending = False
        # In the desktop launcher, records arrive from the bot thread *and*
        # the GUI thread.  The lock keeps drain() from racing an emit() —
        # without it a record could be copied by drain() and then cleared
        # before it was sent.  No awaits ever happen while it is held.
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = bool(value)
            if not value:
                self.buffer.clear()      # `recent` survives: tailing still works
                self.alert_lines.clear()  # stale alarms must not ping after re-enable

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        # Self-recursion guard: while the stream is posting, drop everything
        # from discord.py's own loggers so a failing DM cannot feed back.
        if self.sending and name.startswith(SENDING_GUARD_PREFIXES):
            return
        if name.startswith(EXCLUDED_PREFIXES):
            return
        # discord.http is visible at WARNING+ only (rate limits, HTTP failures)
        # — its DEBUG/INFO request noise would drown the stream.
        if name == "discord.http" and record.levelno < logging.WARNING:
            return
        try:
            line = self.render(record)
            is_alert = self._is_alert(record)
        except Exception:  # pragma: no cover - formatting must never raise
            return
        with self._lock:
            self.recent.append(line)
            if not self._enabled:
                return
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped += 1
            self.buffer.append(line)
            if is_alert:
                self.alert_lines.append(line)

    def _is_alert(self, record: logging.LogRecord) -> bool:
        """Should this line make the operator's phone buzz?"""
        if record.levelno >= self.alert_level:
            return True
        if record.name == "discord.http" and record.levelno >= logging.WARNING:
            message = record.getMessage().lower()
            return any(marker in message for marker in RATE_LIMIT_MARKERS)
        return False

    def tail(self, limit: int = 20) -> list[str]:
        """The most recent lines, without consuming them."""
        with self._lock:
            return list(self.recent)[-max(1, limit):]

    def take_alerts(self) -> Optional[str]:
        """Atomically take the queued alert lines, joined, or None if empty.

        The stream calls this under the cooldown check so that a throttled
        ping never *loses* lines — anything taken here is either sent now or
        was already sent (they are also in `buffer`, so the regular stream
        carries them either way).
        """
        with self._lock:
            if not self.alert_lines:
                return None
            alerts = "\n".join(self.alert_lines)
            self.alert_lines.clear()
            return alerts

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
        with self._lock:
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
        self.handler.alert_level = self._level(config.log_dm_ping_level)
        self._task: Optional[asyncio.Task] = None
        self._user: Optional[discord.User] = None
        self._sent = 0
        self._failures = 0
        self._last_alert_ts = 0.0
        self._alert_cooldown = max(MIN_PING_COOLDOWN, config.log_dm_ping_cooldown_seconds)
        self.disabled_reason: Optional[str] = None

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _level(name: str) -> int:
        level = getattr(logging, str(name).upper(), None)
        if not isinstance(level, int):
            # A typo'd level must not silently become "ping on everything".
            log.warning("Unknown log level %r — using INFO", name)
            return logging.INFO
        return level

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
        while True:
            await asyncio.sleep(interval)
            # A transient bug (bad template, unexpected exception) must never
            # kill the stream for the rest of the event — retry next interval.
            await self._flush_safely()

    async def _flush_safely(self) -> None:
        """`flush()` that can never take the stream (or the bot) down."""
        try:
            await self.flush()
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception:
            log.exception("Live log stream flush failed — will retry")

    # -------------------------------------------------------------- sending

    async def flush(self) -> int:
        """Send everything buffered.  Returns the number of messages posted."""
        if not self.enabled or self._user is None:
            return 0
        await self._maybe_send_alert()
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

    async def _maybe_send_alert(self) -> None:
        """DM an @-ping to the operator when something serious just happened.

        Triggered by lines at `LOG_DM_PING_LEVEL` or worse, or by any Discord
        rate limit.  Pings are throttled to one per `_alert_cooldown`; alert
        lines that arrive during the cooldown stay queued and go out with the
        next permitted ping (they are also in the normal stream, so nothing is
        lost).
        """
        now = time.time()
        if now - self._last_alert_ts < self._alert_cooldown:
            return  # throttled: lines stay queued and go out with the next ping
        alerts = self.handler.take_alerts()
        if alerts is None:
            return
        self._last_alert_ts = now  # count the ping even if the send fails
        content = say(
            TEXT.LOG_ALERT_BODY,
            mention=f"<@{self.target_id}>",
            title=TEXT.LOG_ALERT_TITLE,
            alerts=self._clip(alerts, 1400),
        )
        await self._send(content, ping=True)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """Truncate at a line boundary so the code block stays readable."""
        if len(text) <= limit:
            return text
        cut = text[:limit]
        newline = cut.rfind("\n")
        if newline > 0:
            cut = cut[:newline]
        return f"{cut}\n…"

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

    async def _send(self, content: str, *, ping: bool = False) -> bool:
        if self._user is None:
            return False
        # Pings allow only the operator's own mention; the regular stream
        # permits no mentions at all.
        allowed = (
            discord.AllowedMentions(everyone=False, roles=False, users=[self._user])
            if ping
            else discord.AllowedMentions.none()
        )
        # While we are posting, discord.py's own HTTP/gateway/client records
        # are suppressed so a failed send cannot feed back into the stream.
        self.handler.sending = True
        try:
            if len(content) > 2000:
                content = content[:1999] + "…"
            await self._user.send(content, allowed_mentions=allowed)
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
        finally:
            self.handler.sending = False
