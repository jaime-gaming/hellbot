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

    async def send(self, content=None, **kwargs):
        if self.blocked:
            raise discord.Forbidden(_Resp(), "DMs closed")
        self.sent.append(content or "")


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
    handler.emit(record("http noise", logging.WARNING, name="discord.http"))
    handler.emit(record("real event"))
    lines, _ = handler.drain()
    assert len(lines) == 1 and "real event" in lines[0]


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
