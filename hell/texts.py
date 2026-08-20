"""Loader for `Announcements.py` — the single file that holds every message.

Why a loader instead of a plain import?

* **Hot reload** — `/hell reloadmessages` re-reads the file while the bot is
  running, so wording can be fixed mid-event without a restart.
* **Crash safety** — if the edited file has a syntax error or a missing name,
  the previously loaded text stays in use and the problem is reported instead
  of taking the bot (and a 160-hour run) down.
* **Editable next to the .exe** — a frozen build looks for `Announcements.py`
  beside the executable first, so messages can be changed without rebuilding.

Call sites use the module-level ``TEXT`` proxy, which always resolves to the
currently loaded module::

    from .texts import TEXT
    TEXT.MILESTONE_FOOTER.format(hours=32, remaining_hours=128)

and ``say()`` for a format that cannot explode on a bad placeholder::

    say(TEXT.PROGRESS_TITLE, emoji="🔥")
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from .paths import app_base

log = logging.getLogger("hell.texts")

MODULE_NAME = "Announcements"
FILE_NAME = "Announcements.py"

_module: Optional[ModuleType] = None
_source: Optional[str] = None
_last_error: Optional[str] = None


# ------------------------------------------------------------------ loading


def _load_from_file(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidates() -> list[Path]:
    """Where to look, most-editable first."""
    here = Path(__file__).resolve().parent
    return [app_base() / FILE_NAME, here.parent / FILE_NAME, here / FILE_NAME]


def load(force: bool = False) -> ModuleType:
    """Load (or return the cached) announcements module."""
    global _module, _source, _last_error
    if _module is not None and not force:
        return _module

    errors: list[str] = []
    for path in _candidates():
        if not path.exists():
            continue
        try:
            module = _load_from_file(path)
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        _module, _source, _last_error = module, str(path), None
        sys.modules.setdefault(MODULE_NAME, module)
        log.info("Loaded messages from %s", path)
        return module

    # Last resort: a normal import (covers frozen builds that bundled it).
    try:
        module = importlib.import_module(MODULE_NAME)
        _module, _source, _last_error = module, getattr(module, "__file__", MODULE_NAME), None
        return module
    except Exception as exc:
        errors.append(f"import {MODULE_NAME}: {type(exc).__name__}: {exc}")

    _last_error = "; ".join(errors) or f"{FILE_NAME} not found"
    if _module is not None:
        log.error("Keeping the previously loaded messages — %s", _last_error)
        return _module
    raise ImportError(
        f"Could not load {FILE_NAME}. Looked in: "
        + ", ".join(str(p) for p in _candidates())
        + f". Errors: {_last_error}"
    )


def reload() -> tuple[bool, str]:
    """Re-read the file.  Returns `(ok, message)` and never raises.

    On failure the previously loaded text stays active, so a typo in
    `Announcements.py` can never interrupt a running event.
    """
    global _module, _last_error
    previous = _module
    try:
        for path in _candidates():
            if not path.exists():
                continue
            module = _load_from_file(path)
            _module, _last_error = module, None
            sys.modules[MODULE_NAME] = module
            log.info("Reloaded messages from %s", path)
            _refresh_dependents()
            return True, str(path)
        raise FileNotFoundError(
            f"{FILE_NAME} not found in: " + ", ".join(str(p) for p in _candidates())
        )
    except Exception as exc:
        _module = previous
        _last_error = f"{type(exc).__name__}: {exc}"
        log.error("Reloading %s failed, keeping the previous text: %s", FILE_NAME, _last_error)
        return False, _last_error


def _refresh_dependents() -> None:
    """Rebuild anything that caches values derived from the text file."""
    from . import milestones

    milestones.refresh()


def source() -> str:
    return _source or "(not loaded)"


def last_error() -> Optional[str]:
    return _last_error


def message_count() -> int:
    module = load()
    return sum(1 for name in dir(module) if name.isupper())


# -------------------------------------------------------------------- proxy


class _TextProxy:
    """Attribute access that always hits the freshly loaded module."""

    def __getattr__(self, name: str) -> Any:
        module = load()
        try:
            return getattr(module, name)
        except AttributeError:
            raise AttributeError(
                f"{FILE_NAME} has no message called '{name}'. "
                "Did you rename or delete it? Restore the name (the text can be anything)."
            ) from None

    def __dir__(self):  # pragma: no cover - convenience in a REPL
        return dir(load())


TEXT = _TextProxy()


def say(template: str, /, **values: Any) -> str:
    """`str.format` that degrades gracefully on a bad placeholder.

    A message with an unknown `{name}` is returned unformatted (and logged)
    instead of raising in the middle of an announcement.
    """
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        log.error("Bad placeholder in a message from %s: %s — %r", FILE_NAME, exc, template)
        return template
