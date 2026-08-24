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
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web

log = logging.getLogger("hell.web")

_DOCS = Path(__file__).resolve().parent.parent / "docs"

# All currently connected WebSocket clients.
_clients: set[web.WebSocketResponse] = set()

# The running aiohttp Application (so broadcast() can find the event loop).
_app: Optional[web.Application] = None


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
    try:
        async for msg in ws:
            # We don't expect client messages; ignore them.
            pass
    finally:
        _clients.discard(ws)
        log.debug("WebSocket client disconnected (%d total)", len(_clients))
    return ws


async def _handle_health(request: web.Request) -> web.Response:
    """Simple JSON health probe (no auth, no secrets)."""
    return web.json_response({"ok": True, "clients": len(_clients)})


# --------------------------------------------------------------- app factory


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/dev", _handle_dev)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_get("/health", _handle_health)
    return app


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

    Never raises — a failing client is silently removed.
    """
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
