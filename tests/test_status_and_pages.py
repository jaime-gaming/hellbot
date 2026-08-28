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
    assert data["continuation"] is False
    assert data["total_seconds"] == 576000.0


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

    def mock_spawn_fn(coro, **kwargs):
        coro.close()
        return MagicMock()

    with patch("hell.monitor.get_status_writer"), patch("hell.monitor.spawn", side_effect=mock_spawn_fn) as mock_spawn:
        monitor.sync_status()
        assert mock_spawn.called


def test_push_docs_handles_git_missing(tmp_path):
    with patch("shutil.which", return_value=None):
        result = asyncio.run(push_docs(tmp_path))
        assert result is False


# ------------------------------------------------- live-server discovery file


def test_write_live_server_file(tmp_path, isolate_live_server_file):
    # The autouse fixture redirects writes away from the repo; it yields the
    # directory the file is really written to.
    from hell.pages_sync import write_live_server_file

    path = write_live_server_file(tmp_path, "https://hell.example.com/")
    assert path == isolate_live_server_file / "docs" / "live-server.json"
    data = json.loads(path.read_text())
    assert data["url"] == "https://hell.example.com"  # trailing slash stripped
    assert data["source"] == "hellbot"
    assert "updated" in data


def test_write_live_server_file_empty_url_writes_nothing(tmp_path, isolate_live_server_file):
    from hell.pages_sync import write_live_server_file

    assert write_live_server_file(tmp_path, "   ") is None
    assert not (isolate_live_server_file / "docs" / "live-server.json").exists()


def test_pages_sync_pushes_the_live_server_pointer():
    """The pointer must be committed with the rest of docs/ or GitHub Pages
    never learns where the real bot is listening."""
    from hell.pages_sync import _DOCS_PATHS

    assert "docs/live-server.json" in _DOCS_PATHS


def test_sync_status_publishes_live_server_pointer(engine, config):
    bot = MagicMock()
    monitor = VoiceMonitor(bot, config, engine, MagicMock())
    monitor.config.web_public_url = "https://hell.example.com"
    monitor.config.web_port = 8080

    with patch("hell.monitor.get_status_writer"), \
            patch("hell.monitor.write_live_server_file") as wr, \
            patch("hell.monitor.spawn"):
        monitor.sync_status()

    wr.assert_called_once_with(".", "https://hell.example.com")


def test_sync_status_skips_pointer_when_no_public_url(engine, config):
    bot = MagicMock()
    monitor = VoiceMonitor(bot, config, engine, MagicMock())
    monitor.config.web_public_url = ""

    with patch("hell.monitor.get_status_writer"), \
            patch("hell.monitor.write_live_server_file") as wr, \
            patch("hell.monitor.spawn"):
        monitor.sync_status()

    wr.assert_not_called()


def test_sync_status_skips_pointer_when_web_server_disabled(engine, config):
    bot = MagicMock()
    monitor = VoiceMonitor(bot, config, engine, MagicMock())
    monitor.config.web_public_url = "https://hell.example.com"
    monitor.config.web_port = 0  # dashboard disabled -> the pointer would lie

    with patch("hell.monitor.get_status_writer"), \
            patch("hell.monitor.write_live_server_file") as wr, \
            patch("hell.monitor.spawn"):
        monitor.sync_status()

    wr.assert_not_called()


def test_config_reads_web_public_url(monkeypatch):
    from hell.config import Config

    env = {
        "DISCORD_TOKEN": "t",
        "GUILD_ID": "1",
        "ANNOUNCE_CHANNEL_ID": "2",
        "GAMENIGHT_HOST_ROLE_ID": "3",
        "CLANKER_ROLE_ID": "4",
        "WEB_PUBLIC_URL": " https://hell.example.com ",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env(env_file="/nonexistent/.env")
    assert cfg.web_public_url == "https://hell.example.com"  # trimmed


def test_push_docs_handles_git_commands(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/git"):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # Mock git status --porcelain returning nothing
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = asyncio.run(push_docs(tmp_path))
            assert result is False


def _make_git_repo(tmp_path):
    """A real repo with a bare 'remote' and branch tracking configured."""
    import subprocess

    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()

    def git(*args, cwd=repo):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True)

    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    git("init", "-b", "main")
    git("config", "user.email", "bot@example.com")
    git("config", "user.name", "HellBot")
    git("remote", "add", "origin", str(remote))
    (repo / "docs").mkdir()
    (repo / "docs" / "index.html").write_text("<html>hi</html>\n")
    git("add", "docs/index.html")
    git("commit", "-m", "initial")
    git("config", "branch.main.remote", "origin")
    git("config", "branch.main.merge", "refs/heads/main")
    return repo


def test_push_docs_real_repo_without_live_server_file(tmp_path):
    """No WEB_PUBLIC_URL -> no live-server.json. The push must still work:
    staging a missing path used to break the whole Pages sync."""
    repo = _make_git_repo(tmp_path)
    (repo / "docs" / "status.json").write_text('{"status": "IDLE"}\n')

    assert asyncio.run(push_docs(repo)) is True

    import subprocess
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout
    assert "update live status" in log
    # The commit reached the bare remote.
    remote_log = subprocess.run(
        ["git", "--git-dir", str(tmp_path / "remote.git"), "log", "--oneline", "main"],
        check=True, capture_output=True, text=True).stdout
    assert "update live status" in remote_log


def test_push_docs_real_repo_pushes_live_server_pointer(tmp_path):
    """When the pointer exists it is committed and pushed with the rest."""
    repo = _make_git_repo(tmp_path)
    (repo / "docs" / "status.json").write_text('{"status": "RUNNING"}\n')
    # Written directly: the autouse fixture redirects the helper off-repo.
    (repo / "docs" / "live-server.json").write_text(
        json.dumps({"url": "https://hell.example.com", "updated": "x", "source": "hellbot"}) + "\n"
    )

    assert asyncio.run(push_docs(repo)) is True

    import subprocess
    shown = subprocess.run(
        ["git", "show", "--stat", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True).stdout
    assert "docs/live-server.json" in shown
    assert "docs/status.json" in shown


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

    # Both pages must carry the live-bot connection layer: they discover the
    # real bot (live-server.json / saved server / same origin), probe
    # /health, connect via WebSocket with an HTTP polling fallback, and offer
    # the manual 🔌 override.
    for content in (index_content, dev_content):
        assert "connectFlow" in content
        assert "connSettingsBtn" in content
        assert "live-server.json" in content
        assert "/health" in content or "healthUrlFor" in content
        assert "hellbot.liveServer" in content  # localStorage override key

    # Verify status.json parses as valid JSON
    status_data = json.loads(status_json.read_text())
    assert "event_status" in status_data
    assert "last_updated" in status_data


# ------------------------------------------------- live web server behaviour


@pytest.fixture
def web_app():
    """A fresh web app with clean module state (no leftover provider/snapshot)."""
    from hell import web as hellweb

    hellweb.set_status_provider(None)
    hellweb._last_snapshot = None
    hellweb._snapshot_ts = 0.0
    hellweb.create_app()  # ensure the app builds with clean state
    yield hellweb
    hellweb.set_status_provider(None)
    hellweb._last_snapshot = None
    hellweb._snapshot_ts = 0.0


def test_status_json_serves_live_data_when_provider_registered(web_app):
    """With the bot running, /status.json must be fresh — not the static file
    (which is only rewritten on the 15-minute heartbeat)."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            # No provider: falls back to the static file (GitHub Pages mode).
            async with client.get("/status.json") as r:
                assert r.status == 200
                assert r.headers.get("Cache-Control") == "no-store"
                static_data = await r.json()

            # Register a live provider (what the bot does at startup).
            calls = {"n": 0}

            def provider():
                calls["n"] += 1
                return {"event_status": "RUNNING", "elapsed_seconds": 42.0, "bot_connected": True}

            web_app.set_status_provider(provider)
            async with client.get("/status.json") as r:
                live = await r.json()
            assert live["event_status"] == "RUNNING"
            assert live["elapsed_seconds"] == 42.0
            assert calls["n"] == 1
            assert static_data != live  # genuinely different data sources

            # A failing provider never breaks the endpoint.
            def boom():
                raise RuntimeError("no engine today")

            web_app.set_status_provider(boom)
            async with client.get("/status.json") as r:
                assert r.status == 200
                fallback = await r.json()
            assert fallback == static_data
        finally:
            await client.close()

    asyncio.run(run())


def test_cors_headers_let_other_origins_read_live_data(web_app):
    """The GitHub Pages site is a different origin than the bot's server.
    Without CORS headers the browser would refuse to fetch the bot's live
    ``/status.json`` / ``/health`` — so every response carries them."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            async with client.get("/status.json") as r:
                assert r.status == 200
                assert r.headers["Access-Control-Allow-Origin"] == "*"
            async with client.get("/health") as r:
                assert r.status == 200
                assert r.headers["Access-Control-Allow-Origin"] == "*"
            async with client.get("/") as r:
                assert r.status == 200
                assert r.headers["Access-Control-Allow-Origin"] == "*"
        finally:
            await client.close()

    asyncio.run(run())


def test_options_preflight_answered_with_cors(web_app):
    """Cross-origin browsers send an OPTIONS preflight first; the server must
    answer it (204 + CORS headers) without touching the real handler."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            for route in ("/status.json", "/health", "/ws", "/"):
                async with client.options(route) as r:
                    assert r.status == 204, route
                    assert r.headers["Access-Control-Allow-Origin"] == "*", route
                    assert "GET" in r.headers["Access-Control-Allow-Methods"], route
        finally:
            await client.close()

    asyncio.run(run())


def test_cors_middleware_does_not_break_websocket(web_app):
    """The WS upgrade response must pass through the middleware untouched."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            await web_app.broadcast({"type": "snapshot", "status": "IDLE"})
            # Cross-origin handshake: browsers send an Origin header and the
            # server must accept it (GitHub Pages -> bot's machine).
            ws = await client.ws_connect("/ws", origin="https://jaime-gaming.github.io")
            msg = await asyncio.wait_for(ws.receive(), 1.0)
            assert json.loads(msg.data)["type"] == "snapshot"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_websocket_client_receives_last_snapshot_on_connect(web_app):
    """A newly connected browser renders instantly instead of waiting <=5s."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            # Nothing pushed yet -> no initial message.
            ws = await client.ws_connect("/ws")
            try:
                await asyncio.wait_for(ws.receive(), timeout=0.2)
                raise AssertionError("should not have received anything yet")
            except asyncio.TimeoutError:
                pass
            await ws.close()

            # After a broadcast, the NEXT client gets it immediately.
            await web_app.broadcast({"type": "snapshot", "status": "RUNNING", "elapsed": 7})
            ws2 = await client.ws_connect("/ws")
            msg = await asyncio.wait_for(ws2.receive(), timeout=1.0)
            data = json.loads(msg.data)
            assert data == {"type": "snapshot", "status": "RUNNING", "elapsed": 7}
            await ws2.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_broadcast_remembers_snapshot_without_clients(web_app):
    """The payload is stored even with zero clients (fresh /status.json, new
    connections) — broadcast() must not skip it."""

    async def run():
        assert web_app.client_count() == 0
        await web_app.broadcast({"type": "snapshot", "status": "IDLE"})
        assert web_app._last_snapshot == {"type": "snapshot", "status": "IDLE"}

    asyncio.run(run())


def test_health_reports_live_status_and_snapshot_age(web_app):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        client = TestClient(TestServer(web_app.create_app()))
        await client.start_server()
        try:
            async with client.get("/health") as r:
                data = await r.json()
            assert data["ok"] is True
            assert data["live_status"] is False
            assert data["snapshot_age_seconds"] is None

            web_app.set_status_provider(lambda: {"event_status": "IDLE"})
            await web_app.broadcast({"type": "snapshot"})
            async with client.get("/health") as r:
                data = await r.json()
            assert data["live_status"] is True
            assert data["snapshot_age_seconds"] is not None
        finally:
            await client.close()

    asyncio.run(run())


def test_build_payload_matches_written_file(tmp_path, engine, config, monitor):
    """The build_payload refactor must keep write()'s output identical."""
    from hell.status_writer import get_status

    start_ts = 1_700_000_000.0
    engine.start(
        now=start_ts, guild_id=1, voice_channel_id=config.voice_channel_id,
        announce_channel_id=2, started_by=99,
        initial_participants=[ParticipantRef(1, "Alice")],
    )
    path = tmp_path / "status.json"
    get_status().write(path, engine=engine, monitor=monitor, stream=None, bot=None)
    written = json.loads(path.read_text())

    payload = get_status().build_payload(engine=engine, monitor=monitor, stream=None, bot=None)
    # Same keys and same status (timestamps naturally differ between calls).
    assert set(written) == set(payload)
    assert written["event_status"] == payload["event_status"] == "RUNNING"
    assert written["leaderboard_total"] == payload["leaderboard_total"] == 1
    assert written["voice_channel_id"] == payload["voice_channel_id"] == config.voice_channel_id


def test_websocket_payload_matches_what_the_pages_render(config):
    """The WS payload must keep the exact shape the browser JS renders.

    ``docs/index.html`` only renders WS messages whose ``type`` is
    ``"snapshot"`` and reads a fixed set of keys; ``renderFromStatusJson``
    (the polling fallback) reads the status.json keys.  This pins both
    contracts so neither side can drift silently.
    """
    import asyncio

    from hell import web as hellweb
    from hell.bot import build_bot
    from tests.conftest import T0, obs, start
    from tests.test_integration import FakeTextChannel
    from tests.test_monitor import FakeMember, FakeVoiceChannel

    class _ChanBot:
        def get_channel(self, cid):
            return voice if cid == voice.id else text

    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice"), FakeMember(2, "Bob")])
    bot = build_bot(config)
    try:
        bot.get_channel = _ChanBot().get_channel  # type: ignore[method-assign]
        engine = bot.engine
        start(engine, T0, 1, 2)
        for i in range(1, 61):
            engine.tick(obs(T0 + i, 1, 2))

        asyncio.run(bot._push_web_snapshot())
        payload = hellweb._last_snapshot
        assert payload is not None

        # The gate in the pages' onmessage
        assert payload["type"] == "snapshot"
        # Keys the pages' render() dereferences directly
        for key in (
            "ts", "status", "elapsed", "total", "remaining", "fraction",
            "participants", "paused", "pause_reason", "end_reason", "start_ts",
            "end_ts", "estimated_end_ts", "grace_open", "grace_seconds_left",
            "grace_total", "current_milestone", "upcoming_milestone",
            "milestones", "leaderboard", "leaderboard_total", "alive_check",
            "continuation", "difficulty_level", "difficulty_name",
            "health_errors", "health_warnings", "health_info", "log_tail",
            "rate_limits_5min", "blind_seconds", "active_tasks",
            "operator_dm_ok", "version",
        ):
            assert key in payload, f"the web pages read {key!r} — missing from the WS payload"
        assert payload["status"] == "RUNNING"
        assert payload["participants"] == 2
        assert payload["leaderboard"] and payload["leaderboard"][0]["time"]
        assert all(m["hours"] and "title" in m for m in payload["milestones"])
    finally:
        bot.store.close()
        hellweb._last_snapshot = None
        hellweb._snapshot_ts = 0.0


def test_status_json_payload_matches_what_the_pages_normalize(config):
    """The polling fallback reads these keys via renderFromStatusJson()."""
    from hell.status_writer import get_status
    from tests.conftest import T0, obs, start
    from tests.test_integration import FakeTextChannel
    from tests.test_monitor import FakeMember, FakeVoiceChannel

    class _ChanBot:
        def get_channel(self, cid):
            return voice if cid == voice.id else text

    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice")])
    from hell.bot import build_bot
    bot = build_bot(config)
    try:
        bot.get_channel = _ChanBot().get_channel  # type: ignore[method-assign]
        engine = bot.engine
        start(engine, T0, 1)
        for i in range(1, 30):
            engine.tick(obs(T0 + i, 1))

        payload = get_status().build_payload(
            engine=engine, monitor=bot.monitor, stream=None, bot=bot
        )
        for key in (
            "elapsed_seconds", "total_seconds", "remaining_seconds", "fraction",
            "status", "event_status", "participants", "leaderboard",
            "alive_check", "grace_open", "milestones",
        ):
            assert key in payload, f"renderFromStatusJson reads {key!r} — missing from status.json"
        assert payload["status"] == "RUNNING"
    finally:
        bot.store.close()


def test_start_server_refuses_an_already_bound_port(config):
    """EADDRINUSE must not produce a bare traceback: the server is skipped,
    the runner is cleaned up, and the port works again once it is free."""
    import asyncio
    import socket

    from hell import web as hellweb

    async def run():
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]
        try:
            runner = await hellweb.start_server(port)
            assert runner is None, "port is taken — the server must not start"
        finally:
            blocker.close()
        # Port free again: a normal start succeeds (and must be stopped).
        runner2 = await hellweb.start_server(port)
        assert runner2 is not None
        await hellweb.stop_server(runner2)

    asyncio.run(run())
