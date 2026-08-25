"""Lightweight aiohttp web server that serves live event data over WebSocket.

Two pages:
  /     — public live stats (elapsed, leaderboard, milestones, VC count)
  /dev  — operator diagnostics (errors, warnings, log tail, health report)

The bot calls ``broadcast(payload)`` on every heartbeat (and on significant
events) to push JSON to every connected browser tab.

Design constraints:
  * Never break the bot — every failure is swallowed.
  * No Discord imports — this module is pure asyncio + aiohttp.
  * The server runs alongside the Discord gateway in the same event loop.

Connecting correctly, whichever way the browser reaches us:
  * a WebSocket client receives the last snapshot *immediately* on connect
    (no waiting for the next 5-second push), and then a fresh one every 5s;
  * ``/status.json`` is served LIVE from the bot's current state — the static
    ``docs/status.json`` file (rewritten only every 15-minute heartbeat) is
    now a fallback for when no live provider is registered, e.g. GitHub Pages.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

log = logging.getLogger("hell.web")

_DOCS = Path(__file__).resolve().parent.parent / "docs"

# All currently connected WebSocket clients.
_clients: set[web.WebSocketResponse] = set()

# The running aiohttp Application (so broadcast() can find the event loop).
_app: Optional[web.Application] = None

# The most recent snapshot pushed by the bot (sent instantly to new clients).
_last_snapshot: Optional[dict[str, Any]] = None
_snapshot_ts: float = 0.0

# Builds a fresh status.json-shaped payload on demand (registered by the bot).
_status_provider: Optional[Callable[[], dict[str, Any]]] = None


# ------------------------------------------------------------------ handlers


async def _handle_index(request: web.Request) -> web.FileResponse:
    """Serve the public live-stats page."""
    return web.FileResponse(_DOCS / "index.html")


async def _handle_dev(request: web.Request) -> web.FileResponse:
    """Serve the operator diagnostics page."""
    return web.FileResponse(_DOCS / "dev.html")


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """Accept a WebSocket connection and keep it alive."""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    _clients.add(ws)
    log.debug("WebSocket client connected (%d total)", len(_clients))
    # Send the last snapshot right away so the page renders instantly instead
    # of waiting up to one push interval (5s) for the first data.
    if _last_snapshot is not None:
        try:
            await ws.send_str(json.dumps(_last_snapshot, default=str))
        except Exception:
            log.debug("Could not send the initial snapshot to a new client", exc_info=True)
    try:
        async for _msg in ws:
            # We don't expect client messages; ignore them.
            pass
    finally:
        _clients.discard(ws)
        log.debug("WebSocket client disconnected (%d total)", len(_clients))
    return ws


async def _handle_status_json(request: web.Request) -> web.StreamResponse:
    """Serve the live status when the bot provides it; else the static file."""
    payload: Optional[dict[str, Any]] = None
    if _status_provider is not None:
        try:
            payload = _status_provider()
        except Exception:
            log.exception("Live status provider failed — falling back to the static file")
    if payload is not None:
        resp: web.StreamResponse = web.json_response(payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    path = _DOCS / "status.json"
    if path.exists():
        file_resp: web.StreamResponse = web.FileResponse(path)
        file_resp.headers["Cache-Control"] = "no-store"
        return file_resp
    return web.json_response({"status": "IDLE"})


async def _handle_health(request: web.Request) -> web.Response:
    """Simple JSON health probe (no auth, no secrets)."""
    age = (time.time() - _snapshot_ts) if _snapshot_ts else None
    return web.json_response(
        {
            "ok": True,
            "clients": len(_clients),
            "live_status": _status_provider is not None,
            "snapshot_age_seconds": round(age, 1) if age is not None else None,
        }
    )


# --------------------------------------------------------------- app factory


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/dev", _handle_dev)
    app.router.add_get("/status.json", _handle_status_json)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_get("/health", _handle_health)
    return app


def set_status_provider(provider: Optional[Callable[[], dict[str, Any]]]) -> None:
    """Register the callable that builds a fresh status.json payload.

    Called by the bot at startup; ``None`` disables live serving (the static
    file fallback is used instead, e.g. when only GitHub Pages is serving).
    """
    global _status_provider
    _status_provider = provider


# ------------------------------------------------------------- server lifecycle


async def start_server(port: int = 8080) -> Optional[web.AppRunner]:
    """Start the HTTP + WebSocket server.  Returns the runner (for shutdown)."""
    global _app
    try:
        _app = create_app()
        runner = web.AppRunner(_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("Web server listening on http://0.0.0.0:%d  (pages: / and /dev)", port)
        return runner
    except Exception:
        log.exception("Failed to start web server on port %d", port)
        return None


async def stop_server(runner: Optional[web.AppRunner]) -> None:
    """Gracefully shut down the web server."""
    if runner is None:
        return
    # Close all WebSocket clients so they get a clean disconnect.
    for ws in list(_clients):
        try:
            await ws.close()
        except Exception:
            pass
    _clients.clear()
    try:
        await runner.cleanup()
    except Exception:
        log.debug("Web server cleanup error", exc_info=True)
    log.info("Web server stopped")


# --------------------------------------------------------------- broadcasting


async def broadcast(payload: dict[str, Any]) -> None:
    """Push a JSON payload to every connected WebSocket client.

    The payload is always remembered as the latest snapshot (so new clients
    and ``/status.json`` can serve it) even when nobody is connected.
    Never raises — a failing client is silently removed.
    """
    global _last_snapshot, _snapshot_ts
    _last_snapshot = payload
    _snapshot_ts = time.time()
    if not _clients:
        return
    data = json.dumps(payload, default=str)
    dead: list[web.WebSocketResponse] = []
    for ws in list(_clients):
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def client_count() -> int:
    """How many browsers are currently connected."""
    return len(_clients)
