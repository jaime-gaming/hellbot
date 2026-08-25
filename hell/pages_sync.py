"""Push docs/ to GitHub so GitHub Pages stays current.

Called after every heartbeat write of status.json.  Runs ``git add docs/ &&
git commit && git push`` in a subprocess so it never blocks the event loop.

Failures are logged and swallowed — a broken push must never take the bot down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("hell.pages_sync")

# Only these paths are committed (never secrets, data, or source).
_DOCS_PATHS = ("docs/status.json", "docs/index.html", "docs/dev.html", "docs/live-server.json")

# Pointer file that tells the static GitHub Pages site where the *real* bot
# is listening, so the public dashboard can connect to it live instead of
# showing the stale committed snapshot.
LIVE_SERVER_FILE = "docs/live-server.json"


def write_live_server_file(repo_root: str | Path, url: str) -> Optional[Path]:
    """Write ``docs/live-server.json`` pointing at the bot's public URL.

    The dashboard pages read this file (when it exists) to discover the
    address of the real running bot and connect to it — WebSocket first,
    HTTP polling as a fallback. Returns the written path, or ``None`` when
    the URL is empty (nothing to point at).
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    path = Path(repo_root) / LIVE_SERVER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "hellbot",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


async def push_docs(repo_root: str | Path = ".") -> bool:
    """Commit and push ``docs/`` to the current branch.  Returns True on success.

    Does nothing when git is not installed or the repo has no remote.
    """
    if shutil.which("git") is None:
        log.debug("git not found — skipping Pages sync")
        return False

    root = str(repo_root)

    async def _git(*args: str) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "", "git timed out"
        except Exception as exc:
            return -1, "", str(exc)

    # Check there is something to commit.
    rc, out, _ = await _git("status", "--porcelain", *_DOCS_PATHS)
    if rc != 0 or not out.strip():
        return False  # nothing changed or git failed

    # Stage only the docs files that actually exist (live-server.json is
    # absent when WEB_PUBLIC_URL is not configured; `git add` on a missing
    # path would fail the whole push).
    existing = [p for p in _DOCS_PATHS if (Path(root) / p).exists()]
    if not existing:
        return False
    rc, _, err = await _git("add", *existing)
    if rc != 0:
        log.warning("git add failed: %s", err)
        return False

    # Commit (no-op if nothing staged, which is fine).
    rc, _, err = await _git("commit", "-m", "chore: update live status [skip ci]")
    if rc != 0:
        # exit 1 with empty diff is harmless.
        if "nothing to commit" in err.lower() or "nothing to commit" in out.lower():
            return False
        log.warning("git commit failed: %s", err)
        return False

    # Push.
    rc, _, err = await _git("push")
    if rc != 0:
        log.warning("git push failed: %s", err)
        return False

    log.info("GitHub Pages synced — docs/ pushed")
    return True
