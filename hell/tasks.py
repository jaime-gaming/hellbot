"""Background task helper.

Fire-and-forget `asyncio.create_task` has bitten this project twice: a task
that raises disappears with only a "Task exception was never retrieved" line
at interpreter shutdown, and a task nobody keeps a reference to can be garbage
collected mid-flight.  `spawn()` fixes both — it logs failures loudly and holds
a strong reference until the task finishes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Optional

log = logging.getLogger("hell.tasks")



_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Run `coro` in the background, logging anything it raises."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)

    def done(finished: asyncio.Task) -> None:
        _running.discard(finished)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            log.error("Background task %r failed: %s", name, exc, exc_info=exc)

    task.add_done_callback(done)
    return task


async def cancel(task: Optional[asyncio.Task], *, timeout: float = 5.0) -> None:
    """Cancel a task and wait briefly for it to unwind.  Never raises."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:  # pragma: no cover - defensive
        log.debug("Task %r raised while cancelling", task.get_name(), exc_info=True)


def active() -> int:
    """How many spawned tasks are still running (used by /hell doctor)."""
    return len([t for t in _running if not t.done()])
