"""Live log stream: buffering, batching, delivery and self-protection."""

from __future__ import annotations

import asyncio
import logging

import discord
import pytest

from hell.logsink import DiscordLogHandler, DiscordLogStream
from tests.conftest import T0, make_config, obs, start


def record(msg: str, level: int = logging.INFO, name: str = "hell.monitor") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


class _Resp:
    status = 403
    reason = "Forbidden"


class FakeUser:
    def __init__(self, uid=984083829767675965, *, blocked=False):
        self.id = uid
        self.blocked = blocked
        self.sent: list[str] = []
        self.sent_kwargs: list[dict] = []

    async def send(self, content=None, **kwargs):
        if self.blocked:
            raise discord.Forbidden(_Resp(), "DMs closed")
        self.sent.append(content or "")
        self.sent_kwargs.append(kwargs)


class FakeBot:
    def __init__(self, user=None):
        self.user_obj = user or FakeUser()

    def get_user(self, uid):
        return self.user_obj if self.user_obj.id == uid else None


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def stream(config):
    bot = FakeBot()
    stream = DiscordLogStream(bot, config)
    stream._user = bot.user_obj          # skip the fetch round-trip
    return stream, bot.user_obj


# ---------------------------------------------------------------- handler

def test_default_recipient_is_the_operator(config):
    assert config.log_dm_user_id == 984083829767675965


def test_handler_renders_a_readable_line():
    handler = DiscordLogHandler()
    line = handler.render(record("➕ Alice joined the VC (3 valid human(s) inside)"))
    assert "•" in line and "[monitor]" in line
    assert "Alice joined the VC" in line


def test_level_icons_distinguish_errors():
    handler = DiscordLogHandler()
    assert "❌" in handler.render(record("boom", logging.ERROR))
    assert "⚠️" in handler.render(record("careful", logging.WARNING))
    assert "💥" in handler.render(record("fatal", logging.CRITICAL))


def test_handler_never_mirrors_its_own_output():
    """Otherwise a failing DM logs an error, which tries to DM, which fails…"""
    handler = DiscordLogHandler()
    handler.emit(record("recursion!", logging.ERROR, name="hell.logsink"))
    handler.emit(record("debug http", logging.DEBUG, name="discord.http"))
    handler.emit(record("real event"))
    lines, _ = handler.drain()
    assert len(lines) == 1 and "real event" in lines[0]


def test_rate_limit_warnings_are_visible_when_idle():
    """discord.http WARNINGs (rate limits, HTTP failures) now reach the stream."""
    handler = DiscordLogHandler()
    handler.emit(
        record(
            "We are being rate limited. Retrying in 2.50 seconds.", logging.WARNING,
            name="discord.http",
        )
    )
    lines, _ = handler.drain()
    assert len(lines) == 1 and "rate limited" in lines[0]


def test_http_noise_while_sending_is_suppressed():
    """While the stream is posting, discord.py's own warnings must not feed back."""
    handler = DiscordLogHandler()
    handler.sending = True
    handler.emit(
        record(
            "We are being rate limited. Retrying in 2.50 seconds.", logging.WARNING,
            name="discord.http",
        )
    )
    handler.emit(record("real event"))
    lines, _ = handler.drain()
    assert len(lines) == 1 and "real event" in lines[0]


# -------------------------------------------------------------------- alerts


def test_error_lines_are_flagged_as_alerts():
    handler = DiscordLogHandler()
    handler.emit(record("boom", logging.ERROR))
    handler.emit(record("fatal", logging.CRITICAL))
    handler.emit(record("info", logging.INFO))
    assert len(handler.alert_lines) == 2


def test_rate_limit_warnings_are_alerts_even_below_the_ping_level():
    """Rate limits ping regardless of LOG_DM_PING_LEVEL — they are exactly
    the 'something is wrong' the operator asked to be woken for."""
    handler = DiscordLogHandler()
    handler.alert_level = logging.CRITICAL
    handler.emit(
        record(
            "We are being rate limited. Retrying in 2.50 seconds.", logging.WARNING,
            name="discord.http",
        )
    )
    handler.emit(record("plain http warning", logging.WARNING, name="discord.http"))
    assert len(handler.alert_lines) == 1


def test_alert_lines_are_cleared_when_the_stream_is_disabled():
    """Stale alarms must not ping later after a /hell logs off -> on cycle."""
    handler = DiscordLogHandler()
    handler.emit(record("boom", logging.ERROR))
    assert handler.alert_lines
    handler.set_enabled(False)
    assert not handler.alert_lines


def test_flush_sends_a_ping_with_the_operator_mention(stream):
    stream, user = stream
    stream.handler.emit(record("boom", logging.ERROR))
    run(stream.flush())
    assert any("<@984083829767675965>" in message for message in user.sent)
    assert any("something went wrong" in message for message in user.sent)
    # The alert goes out first and is the only message that may mention anyone.
    allowed = user.sent_kwargs[0]["allowed_mentions"]
    assert allowed.users and not allowed.everyone and not allowed.roles


def test_normal_stream_never_mentions_anyone(stream):
    stream, user = stream
    stream.handler.emit(record("event 1"))
    run(stream.flush())
    for kwargs in user.sent_kwargs:
        allowed = kwargs["allowed_mentions"]
        assert not allowed.users and not allowed.everyone and not allowed.roles


def test_pings_are_throttled_by_the_cooldown(stream):
    stream, user = stream
    stream._alert_cooldown = 60.0
    stream.handler.emit(record("boom 1", logging.ERROR))
    run(stream.flush())
    assert len(user.sent) == 2                        # ping + code block
    user.sent.clear()

    stream.handler.emit(record("boom 2", logging.ERROR))
    run(stream.flush())                               # inside the cooldown
    assert not any("<@984083829767675965>" in m for m in user.sent)
    assert len(stream.handler.alert_lines) == 1       # queued, not lost

    stream._last_alert_ts = 0.0                       # cooldown elapses
    run(stream.flush())
    assert any("<@984083829767675965>" in m for m in user.sent)


def test_alert_ping_level_is_configurable(tmp_path):
    config = make_config(tmp_path, log_dm_ping_level="WARNING")
    bot = FakeBot()
    stream = DiscordLogStream(bot, config)
    stream._user = bot.user_obj
    stream.handler.emit(record("careful", logging.WARNING, name="hell.monitor"))
    run(stream.flush())
    assert any("<@984083829767675965>" in m for m in bot.user_obj.sent)
    assert bot.user_obj.sent_kwargs[0]["allowed_mentions"].users


def test_handler_can_be_switched_off():
    handler = DiscordLogHandler()
    handler.set_enabled(False)
    handler.emit(record("ignored"))
    assert handler.drain() == ([], 0)


def test_overflow_is_counted_not_crashed():
    handler = DiscordLogHandler(capacity=10)
    for i in range(25):
        handler.emit(record(f"line {i}"))
    lines, dropped = handler.drain()
    assert len(lines) == 10 and dropped == 15
    assert "line 24" in lines[-1]         # newest survive


def test_long_lines_are_truncated():
    handler = DiscordLogHandler()
    handler.emit(record("x" * 5000))
    line, _ = handler.drain()
    assert len(line[0]) <= 501


def test_exception_tracebacks_are_included():
    handler = DiscordLogHandler()
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys

        rec = logging.LogRecord("hell.bot", logging.ERROR, __file__, 1, "crashed", None, sys.exc_info())
    assert "ValueError" in handler.render(rec)


# ----------------------------------------------------------------- batching

def test_lines_are_packed_into_message_sized_chunks():
    lines = ["y" * 200 for _ in range(40)]
    chunks = DiscordLogStream.chunks(lines)
    assert len(chunks) > 1
    assert all(len(c) <= 1900 for c in chunks)


def test_single_monster_line_is_clamped():
    chunks = DiscordLogStream.chunks(["z" * 5000])
    assert all(len(c) <= 1900 for c in chunks)


# ----------------------------------------------------------------- delivery

def test_flush_sends_buffered_lines(stream):
    stream, user = stream
    for i in range(5):
        stream.handler.emit(record(f"event {i}"))
    assert run(stream.flush()) == 1
    assert "event 0" in user.sent[0] and "event 4" in user.sent[0]
    assert user.sent[0].startswith("```")


def test_flush_is_a_noop_when_nothing_happened(stream):
    stream, user = stream
    assert run(stream.flush()) == 0
    assert user.sent == []


def test_dropped_lines_are_reported(stream):
    stream, user = stream
    stream.handler.buffer = __import__("collections").deque(maxlen=5)
    for i in range(12):
        stream.handler.emit(record(f"spam {i}"))
    run(stream.flush())
    assert "dropped" in user.sent[0]


def test_burst_is_capped_to_avoid_rate_limits(stream):
    stream, user = stream
    for i in range(400):                       # way more than 3 messages worth
        stream.handler.emit(record(f"{'q' * 200} {i}"))
    posted = run(stream.flush())
    assert posted <= 3
    assert "suppressed" in user.sent[-1]


def test_closed_dms_disable_the_stream(config):
    blocked = FakeUser(blocked=True)
    stream = DiscordLogStream(FakeBot(blocked), config)
    stream._user = blocked
    stream.handler.emit(record("hello"))
    run(stream.flush())
    assert not stream.enabled
    assert "DMs closed" in (stream.disabled_reason or "")


def test_status_reports_target_and_level(stream):
    stream, _user = stream
    assert "984083829767675965" in stream.status()
    stream.set_level("WARNING")
    assert "WARNING" in stream.status()
    stream.set_enabled(False)
    assert stream.status() == "off"


def test_disabled_by_config(tmp_path):
    config = make_config(tmp_path, log_dm_enabled=False)
    stream = DiscordLogStream(FakeBot(), config)
    run(stream.start())
    assert not stream.running
    assert "LOG_DM_ENABLED=false" in (stream.disabled_reason or "")


def test_attach_and_detach_the_root_handler(config):
    stream = DiscordLogStream(FakeBot(), config)
    previous = logging.getLogger().level
    stream.attach()
    try:
        assert stream.handler in logging.getLogger().handlers
        logging.getLogger("hell.test").info("captured by the stream")
        lines, _ = stream.handler.drain()
        assert any("captured by the stream" in line for line in lines)
    finally:
        stream.detach()
        logging.getLogger().setLevel(previous)
    assert stream.handler not in logging.getLogger().handlers


def test_attaching_widens_the_root_level_so_info_is_visible(config):
    """LOG_LEVEL=WARNING must not silence the INFO stream the operator asked for."""
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.WARNING)
    stream = DiscordLogStream(FakeBot(), config)
    stream.attach()
    try:
        assert root.level == logging.INFO
        stream.set_level("DEBUG")
        assert root.level == logging.DEBUG
    finally:
        stream.detach()
        root.setLevel(previous)


# ------------------------------------------------- what the operator sees

def test_joins_and_leaves_are_logged_in_real_time(config, engine, caplog, monkeypatch):
    """The stream's readability depends on these lines existing."""
    from hell.announcer import Announcer
    from hell.monitor import VoiceMonitor
    from tests.test_monitor import FakeBot as MonitorBot
    from tests.test_monitor import FakeMember, FakeVoiceChannel

    bot = MonitorBot()
    monitor = VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))
    alice, bob = FakeMember(1, "Alice"), FakeMember(2, "Bob")
    channel = FakeVoiceChannel([alice, bob])
    monkeypatch.setattr(monitor, "voice_channel", lambda: channel)
    start(engine, T0, 1, 2)

    with caplog.at_level(logging.INFO, logger="hell.monitor"):
        humans, _ = run(monitor.collect())
        monitor._log_presence_changes(humans)
        channel.members = [alice]
        humans, _ = run(monitor.collect())
        monitor._log_presence_changes(humans)
        channel.members = [alice, bob, FakeMember(3, "Cara")]
        humans, _ = run(monitor.collect())
        monitor._log_presence_changes(humans)

    text = "\n".join(caplog.messages)
    assert "Alice joined the VC" in text
    assert "Bob left the VC" in text
    assert "Cara joined the VC" in text


def test_engine_activity_reaches_the_stream(config, engine):
    """Grace warnings, failures and milestones all flow through logging."""
    stream = DiscordLogStream(FakeBot(), config)
    stream.attach()
    try:
        start(engine, T0, 1)
        engine.tick(obs(T0 + 60))                      # VC empties -> warning
        engine.tick(obs(T0 + 60 + 15))                 # grace expires -> FAILED
        lines, _ = stream.handler.drain()
    finally:
        stream.detach()
    text = "\n".join(lines)
    assert "VC is EMPTY" in text
    assert "FAILED" in text


def test_suppressed_count_reports_lines_not_chunks(stream):
    """Regression: the overflow notice used to mix up chunks and lines."""
    stream, user = stream
    for i in range(300):
        stream.handler.emit(record(f"{'w' * 150} {i}"))
    run(stream.flush())
    notice = user.sent[-1]
    assert "suppressed" in notice
    count = int(notice.split()[1])
    assert count > 3          # lines, not the 1-3 chunks that were skipped


# ----------------------------------------------------- production hardening

def test_take_alerts_drains_atomically():
    handler = DiscordLogHandler()
    assert handler.take_alerts() is None
    handler.emit(record("boom 1", logging.ERROR))
    handler.emit(record("boom 2", logging.ERROR))
    alerts = handler.take_alerts()
    assert alerts is not None
    assert "boom 1" in alerts and "boom 2" in alerts
    assert not handler.alert_lines          # taken, not left behind
    assert handler.take_alerts() is None    # empty now


def test_clip_truncates_at_a_line_boundary():
    assert DiscordLogStream._clip("short", 100) == "short"
    text = "\n".join(f"line {i}" for i in range(500))
    clipped = DiscordLogStream._clip(text, 200)
    assert len(clipped) <= 202
    assert clipped.endswith("…")
    # Every kept line is a complete line, never a chopped fragment.
    for kept in clipped[:-1].split("\n"):
        assert kept in text


def test_concurrent_emit_and_drain_lose_no_lines():
    """Regression: drain() used to copy-then-clear, so a record emitted
    in between the two steps was lost.  The lock closes that window."""
    import threading
    import time as _time

    handler = DiscordLogHandler(capacity=500_000)
    stop = threading.Event()
    emitted = 0

    def producer():
        nonlocal emitted
        while not stop.is_set():
            for _ in range(5):
                handler.emit(record("thread line", logging.INFO))
                emitted += 1
            _time.sleep(0.0001)

    thread = threading.Thread(target=producer)
    thread.start()
    collected = 0
    for _ in range(100):
        lines, _ = handler.drain()
        collected += len(lines)
        _time.sleep(0.001)
    stop.set()
    thread.join()
    lines, _ = handler.drain()
    collected += len(lines)
    assert collected == emitted


def test_flush_exception_is_contained(stream):
    """A broken flush must never propagate out of the stream loop."""
    stream, _ = stream

    async def boom():
        raise RuntimeError("kaboom")

    stream.flush = boom  # type: ignore[assignment]
    run(stream._flush_safely())     # must not raise


def test_run_survives_transient_flush_errors(stream, monkeypatch):
    """One bad flush logs and retries instead of killing the stream."""
    stream, _ = stream
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        raise asyncio.CancelledError  # end the loop after surviving

    original_sleep = asyncio.sleep

    async def quick_sleep(_):
        await original_sleep(0)

    monkeypatch.setattr(stream, "flush", flaky)
    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(stream._run())
    assert len(calls) >= 3            # it kept going past the failures


def test_unknown_level_falls_back_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="hell.logsink"):
        assert DiscordLogStream._level("BANANA") == logging.INFO
    assert any("BANANA" in r.message for r in caplog.records)
