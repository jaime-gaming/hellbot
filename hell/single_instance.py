"""Single-instance guard: refuse a second live process against the same database.

Two processes logged in with the same token are a hazard, not just a nuisance:

* Discord interactions reach both sessions — one of them answers, the other
  gets ``400 (40117: Interaction Failed)`` on every ``/hell`` command.
* Both monitors tick the same voice channel, so VC time gets credited twice.
* Both bind the web-dashboard port — the loser dies with ``EADDRINUSE``.

The classic cause is a stale copy next to the live one: an old Docker
container that was never stopped, a systemd service plus a manual run, or a
GUI launcher left open while a second instance starts.

The lock is a kernel-managed ``flock(2)`` (``msvcrt.locking`` on Windows) on
a file next to the database, so it is released automatically when the
process dies — crashes, ``kill -9`` and container stops leave no stale lock
behind. There is deliberately no PID-based staleness logic: the kernel
already handles it correctly.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import IO, Optional

__all__ = ["LOCK_NAME", "InstanceLock", "SingleInstanceError"]

LOCK_NAME = "hellbot.lock"


class SingleInstanceError(RuntimeError):
    """Another process already holds the instance lock for this database."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        super().__init__(
            "Another copy of the bot is already running (lock: "
            f"{lock_path}). Stop the other instance first (e.g. `docker ps` "
            "then `docker stop <container>`, or `ps aux | grep bot`) — two "
            "instances double-credit time and fight over every command."
        )


class InstanceLock:
    """Exclusive, non-blocking process lock bound to one database directory.

    ``acquire()`` raises :class:`SingleInstanceError` when another process
    already holds the lock.  The guard is best-effort: if the lock file
    simply cannot be created (read-only mount, permissions), the bot starts
    without a lock rather than refusing to run at all.
    """

    def __init__(self, database_path: Path):
        self.path = Path(database_path).parent / LOCK_NAME
        self._fh: Optional[IO[bytes]] = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        if self.held:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Binary mode: byte locking (msvcrt) and PID writing both need
            # exact offsets, and text-mode newline translation breaks them.
            fh = open(self.path, "a+b")
        except OSError:
            return  # no lock, but the bot still starts
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # Windows: lock the first byte
                import msvcrt

                fh.seek(0)
                if fh.read(1) == b"":
                    fh.write(b"\0")
                    fh.flush()
                    fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            fh.close()
            raise SingleInstanceError(self.path) from exc
        self._write_pid(fh)
        self._fh = fh

    def _write_pid(self, fh: IO[bytes]) -> None:
        """Best-effort: record our PID so `cat hellbot.lock` is diagnostic."""
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:
            pass

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        finally:
            self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
