"""Push docs/ to GitHub so GitHub Pages stays current.

Called after every heartbeat write of status.json.  Runs ``git add docs/ &&
git commit && git push`` in a subprocess so it never blocks the event loop.

Failures are logged and swallowed — a broken push must never take the bot down.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger("hell.pages_sync")

# Only these paths are committed (never secrets, data, or source).
_DOCS_PATHS = ("docs/status.json", "docs/index.html", "docs/dev.html")


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

    # Stage only the docs files.
    rc, _, err = await _git("add", *_DOCS_PATHS)
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
