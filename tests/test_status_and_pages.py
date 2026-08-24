"""Tests for docs/status.json, StatusFile, pages_sync, and HTML frontend assets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hell.engine import HellEngine
from hell.models import EventStatus, ParticipantRef
from hell.monitor import VoiceMonitor
from hell.pages_sync import push_docs
from hell.status_writer import StatusFile, write_status
from hell.storage import Store


@pytest.fixture
def temp_store(tmp_path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


@pytest.fixture
def engine(temp_store, config):
    return HellEngine(temp_store, config)


@pytest.fixture
def monitor(engine, config):
    bot = MagicMock()
    announcer = MagicMock()
    return VoiceMonitor(bot, config, engine, announcer)


def test_status_file_errors_and_warnings():
    sf = StatusFile()
    sf.add_error("Something went wrong")
    sf.add_warning("High latency detected")

    snap = sf.snapshot(bot_connected=True, event_status="RUNNING")
    assert snap["bot_connected"] is True
    assert snap["event_status"] == "RUNNING"
    assert len(snap["errors_last_24h"]) == 1
    assert "Something went wrong" in snap["errors_last_24h"][0]
    assert len(snap["warnings_last_24h"]) == 1
    assert "High latency detected" in snap["warnings_last_24h"][0]


def test_status_file_self_healing():
    sf = StatusFile()
    # Add an error formatted with an old timestamp
    sf._errors.append("[2020-01-01 00:00:00 UTC] very old error")
    sf._warnings.append("[2020-01-01 00:00:00 UTC] very old warning")

    sf.clear_stale_errors()
    assert len(sf._errors) == 0
    assert len(sf._warnings) == 0


def test_write_status_idle(tmp_path, engine, monitor):
    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    assert path.exists()
    data = json.loads(path.read_text())
    assert data["event_status"] == "IDLE"
    assert data["status"] == "IDLE"
    assert data["paused"] is False
    assert data["participants"] == 0
    assert data["elapsed_seconds"] == 0.0
    assert data["total_seconds"] == 576000.0


def test_write_status_running(tmp_path, engine, monitor):
    engine.start(
        now=1000.0,
        guild_id=1,
        voice_channel_id=2,
        announce_channel_id=3,
        started_by=4,
        initial_participants=[ParticipantRef(10, "Alice"), ParticipantRef(20, "Bob")],
    )

    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    data = json.loads(path.read_text())
    assert data["event_status"] == "RUNNING"
    assert data["status"] == "RUNNING"
    assert data["paused"] is False
    assert data["participants"] == 2
    assert data["start_ts"] == 1000.0


def test_write_status_paused(tmp_path, engine, monitor):
    engine.start(
        now=1000.0,
        guild_id=1,
        voice_channel_id=2,
        announce_channel_id=3,
        started_by=4,
        initial_participants=[ParticipantRef(10, "Alice")],
    )
    engine.pause(now=1050.0, reason="maintenance")

    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    data = json.loads(path.read_text())
    assert data["event_status"] == "RUNNING"
    assert data["paused"] is True
    assert data["pause_reason"] == "maintenance"


def test_write_status_failed(tmp_path, engine, monitor):
    engine.start(
        now=1000.0,
        guild_id=1,
        voice_channel_id=2,
        announce_channel_id=3,
        started_by=4,
        initial_participants=[ParticipantRef(10, "Alice")],
    )
    from hell.engine import Observation
    # Tick empty VC to trigger grace, then expire grace
    engine.tick(Observation(now=1001.0, participants=()))
    engine.tick(Observation(now=1020.0, participants=()))

    assert engine.status is EventStatus.FAILED

    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    data = json.loads(path.read_text())
    assert data["event_status"] == "FAILED"
    assert data["status"] == "FAILED"
    assert data["end_reason"] is not None
    assert "empty" in data["end_reason"].lower()


def test_write_status_stopped(tmp_path, engine, monitor):
    engine.start(
        now=1000.0,
        guild_id=1,
        voice_channel_id=2,
        announce_channel_id=3,
        started_by=4,
        initial_participants=[ParticipantRef(10, "Alice")],
    )
    engine.cancel(now=1050.0, by_user_id=4)

    assert engine.status is EventStatus.CANCELLED

    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    data = json.loads(path.read_text())
    assert data["event_status"] == "CANCELLED"
    assert data["status"] == "CANCELLED"


def test_write_status_completed(tmp_path, engine, monitor):
    engine.start(
        now=1000.0,
        guild_id=1,
        voice_channel_id=2,
        announce_channel_id=3,
        started_by=4,
        initial_participants=[ParticipantRef(10, "Alice")],
    )
    from hell.engine import Observation
    # Tick to 160h
    engine.tick(Observation(now=1000.0 + 576000.0, participants=(ParticipantRef(10, "Alice"),)))

    assert engine.status is EventStatus.COMPLETED

    path = tmp_path / "status.json"
    write_status(path=str(path), engine=engine, monitor=monitor)

    data = json.loads(path.read_text())
    assert data["event_status"] == "COMPLETED"
    assert data["status"] == "COMPLETED"


def test_voice_monitor_sync_status_triggers_pages_sync(tmp_path, engine, monitor):
    monitor.config.github_pages_sync = True
    async def fake_push():
        return True

    with patch("hell.monitor.spawn") as mock_spawn, patch("hell.monitor.push_docs", side_effect=fake_push):
        monitor.sync_status()
        assert mock_spawn.called


def test_push_docs_handles_git_missing(tmp_path):
    with patch("shutil.which", return_value=None):
        result = asyncio.run(push_docs(tmp_path))
        assert result is False


def test_push_docs_handles_git_commands(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/git"):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # Mock git status --porcelain returning nothing
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = asyncio.run(push_docs(tmp_path))
            assert result is False


def test_html_files_structure():
    docs_dir = Path("docs")
    index_html = docs_dir / "index.html"
    dev_html = docs_dir / "dev.html"
    status_json = docs_dir / "status.json"

    assert index_html.exists()
    assert dev_html.exists()
    assert status_json.exists()

    index_content = index_html.read_text()
    dev_content = dev_html.read_text()

    # Verify key elements in index.html
    assert "statusBadge" in index_content
    assert "graceBanner" in index_content
    assert "pausedBanner" in index_content
    assert "idleBanner" in index_content
    assert "failedBanner" in index_content
    assert "stoppedBanner" in index_content
    assert "completedBanner" in index_content
    assert "progressBar" in index_content
    assert "vcCount" in index_content
    assert "lbBody" in index_content
    assert "aliveCard" in index_content
    assert "renderFromStatusJson" in index_content

    # Verify dev.html elements
    assert "statusBadge" in dev_content
    assert "evStatus" in dev_content
    assert "evElapsed" in dev_content
    assert "evVc" in dev_content
    assert "renderFromStatusJson" in dev_content

    # Verify status.json parses as valid JSON
    status_data = json.loads(status_json.read_text())
    assert "event_status" in status_data
    assert "last_updated" in status_data
