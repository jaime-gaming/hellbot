"""Launcher tests: config file handling and the bot supervisor thread.

No tkinter is needed here — the GUI is a thin shell over these two modules.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from hell.config import ConfigError
from launcher import envfile
from launcher.runtime import ERROR, RUNNING, STOPPED, BotSupervisor, QueueLogHandler


# ------------------------------------------------------------------ envfile

GOOD = {
    "DISCORD_TOKEN": "token-value",
    "GUILD_ID": "111",
    "VOICE_CHANNEL_ID": "1539756705997652079",
    "ANNOUNCE_CHANNEL_ID": "222",
    "GAMENIGHT_HOST_ROLE_ID": "333",
    "CLANKER_ROLE_ID": "444",
}


def write_good_env(tmp_path: Path) -> Path:
    values = envfile.read_env(tmp_path / ".env")
    values.update(GOOD)
    return envfile.write_env(tmp_path / ".env", values)


def test_defaults_when_no_file(tmp_path):
    values = envfile.read_env(tmp_path / "missing.env")
    assert values["VOICE_CHANNEL_ID"] == "1539756705997652079"
    assert values["PROGRESS_INTERVAL"] == "10"


def test_write_then_read_round_trip(tmp_path):
    path = write_good_env(tmp_path)
    values = envfile.read_env(path)
    for key, expected in GOOD.items():
        assert values[key] == expected
    assert envfile.validate(values) == []


def test_written_file_has_no_inline_comments(tmp_path):
    """Regression: inline comments used to be parsed back as values."""
    path = write_good_env(tmp_path)
    for line in path.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        assert "#" not in line.split("=", 1)[1]


def test_validation_catches_missing_and_non_numeric(tmp_path):
    values = envfile.read_env(tmp_path / ".env")
    values.update(GOOD)
    values["DISCORD_TOKEN"] = ""
    values["GUILD_ID"] = "not-a-number"
    problems = envfile.validate(values)
    assert any("DISCORD_TOKEN" in p for p in problems)
    assert any("GUILD_ID" in p for p in problems)


def test_unknown_keys_are_preserved(tmp_path):
    values = envfile.read_env(tmp_path / ".env")
    values.update(GOOD)
    values["MY_CUSTOM_KEY"] = "keep-me"
    path = envfile.write_env(tmp_path / ".env", values)
    assert envfile.read_env(path)["MY_CUSTOM_KEY"] == "keep-me"


def test_write_is_atomic(tmp_path):
    path = write_good_env(tmp_path)
    assert not (tmp_path / ".env.tmp").exists()
    assert path.exists()


def test_mask_hides_the_token():
    masked = envfile.mask("supersecrettoken")
    assert masked.startswith("supe") and "secret" not in masked


# ------------------------------------------------------------- log handler

def test_queue_log_handler_drains_and_bounds():
    handler = QueueLogHandler(maxsize=5)
    import logging

    handler.setFormatter(logging.Formatter("%(message)s"))
    for i in range(20):
        handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, f"line {i}", None, None))
    lines = handler.drain()
    assert len(lines) == 5           # bounded
    assert lines[-1] == "line 19"    # newest kept


# -------------------------------------------------------------- supervisor

class FakeBot:
    """Stands in for HellBot: starts, waits, closes."""

    def __init__(self, config):
        self.config = config
        self.user = "FakeBot#0001"
        self.engine = None
        self.health = None
        self._closed = asyncio.Event()
        self._is_closed = False

    async def start(self, token):
        assert token == GOOD["DISCORD_TOKEN"]
        await self._closed.wait()

    async def close(self):
        self._is_closed = True
        self._closed.set()

    def is_closed(self):
        return self._is_closed


class ExplodingBot(FakeBot):
    async def start(self, token):
        raise RuntimeError("Improper token has been passed.")


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_supervisor_start_and_stop(tmp_path):
    path = write_good_env(tmp_path)
    sup = BotSupervisor(env_file=path, bot_factory=FakeBot)
    sup.start()
    assert wait_for(lambda: sup.status == RUNNING), f"status stuck at {sup.status}"
    stats = sup.stats()
    assert stats.connected_as == "FakeBot#0001"
    sup.stop(timeout=5)
    assert sup.status == STOPPED
    assert not sup.is_running


def test_supervisor_reports_bad_token(tmp_path):
    path = write_good_env(tmp_path)
    sup = BotSupervisor(env_file=path, bot_factory=ExplodingBot)
    sup.start()
    assert wait_for(lambda: sup.status == ERROR)
    assert "token" in sup.stats().detail.lower()


def test_supervisor_refuses_incomplete_config(tmp_path):
    values = envfile.read_env(tmp_path / ".env")
    values.update(GOOD)
    values["DISCORD_TOKEN"] = ""
    path = envfile.write_env(tmp_path / ".env", values)
    sup = BotSupervisor(env_file=path, bot_factory=FakeBot)
    with pytest.raises(ConfigError):
        sup.start()
    assert sup.status == STOPPED


def test_supervisor_cannot_double_start(tmp_path):
    path = write_good_env(tmp_path)
    sup = BotSupervisor(env_file=path, bot_factory=FakeBot)
    sup.start()
    assert wait_for(lambda: sup.status == RUNNING)
    with pytest.raises(RuntimeError):
        sup.start()
    sup.stop(timeout=5)


def test_stats_read_last_event_state_while_stopped(tmp_path, monkeypatch):
    """The dashboard shows the saved event even before the bot connects."""
    from hell.models import EventStatus
    from hell.storage import Store

    db = tmp_path / "hell.sqlite3"
    store = Store(db)
    state = store.load_state()
    state.status = EventStatus.RUNNING
    state.event_uid = "uid"
    state.start_ts = time.time() - 3600
    store.save_state(state)
    store.close()

    values = envfile.read_env(tmp_path / ".env")
    values.update(GOOD)
    values["DATABASE_PATH"] = str(db)
    path = envfile.write_env(tmp_path / ".env", values)

    sup = BotSupervisor(env_file=path, bot_factory=FakeBot)
    stats = sup.stats()
    assert stats.event_status == "RUNNING"
    assert 3500 < stats.elapsed < 3700


def test_inline_comments_are_not_parsed_as_values(tmp_path):
    """Regression: copying .env.example gave `MONITOR_INTERVAL=1   # cadence`."""
    path = tmp_path / ".env"
    path.write_text(
        "DISCORD_TOKEN=abc\n"
        "GUILD_ID=111   # the server\n"
        "MONITOR_INTERVAL=1\t# cadence\n"
        'ANNOUNCE_CHANNEL_ID="222"\n',
        encoding="utf-8",
    )
    values = envfile.read_env(path)
    assert values["GUILD_ID"] == "111"
    assert values["MONITOR_INTERVAL"] == "1"
    assert values["ANNOUNCE_CHANNEL_ID"] == "222"
    assert values["DISCORD_TOKEN"] == "abc"


def test_shipped_env_example_parses_cleanly():
    """The example file must be usable as-is after filling in the blanks."""
    from hell.paths import app_base

    example = app_base() / ".env.example"
    values = envfile.read_env(example)
    values.update(GOOD)
    assert envfile.validate(values) == []


class ReadyAwareBot(FakeBot):
    """A bot that only becomes ready when told to (like the real gateway)."""

    def __init__(self, config):
        super().__init__(config)
        self._ready = asyncio.Event()

    async def wait_until_ready(self):
        await self._ready.wait()

    async def start(self, token):
        await asyncio.sleep(0.05)
        self._ready.set()
        await self._closed.wait()


def test_status_only_says_running_once_discord_accepts_us(tmp_path):
    """Regression: the dashboard used to claim RUNNING while still connecting."""
    path = write_good_env(tmp_path)
    sup = BotSupervisor(env_file=path, bot_factory=ReadyAwareBot)
    sup.start()
    assert sup.status == "STARTING"
    assert wait_for(lambda: sup.status == RUNNING), "never reached RUNNING"
    sup.stop(timeout=5)
    assert sup.status == STOPPED
