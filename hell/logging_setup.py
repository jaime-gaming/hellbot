"""Logging setup: rotating file + console, safe when there is no console.

A GUI/`pythonw`/`--noconsole` process has `sys.stderr is None`; attaching a
StreamHandler to it raises at the first log record.  This module handles that,
which is exactly what the desktop launcher needs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

from .paths import log_file

FORMAT = "%(asctime)s %(levelname)-8s %(name)-16s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# Console colours, used only on a real terminal (never in the log file, and
# never when the output is piped or redirected).
RESET = "\033[0m"
LEVEL_COLOR = {
    logging.DEBUG: "\033[2;37m",     # dim grey
    logging.INFO: "\033[38;5;208m",  # ember orange
    logging.WARNING: "\033[33m",     # yellow
    logging.ERROR: "\033[31m",       # red
    logging.CRITICAL: "\033[1;41m",  # white on red
}


class ColourFormatter(logging.Formatter):
    """Adds colour to the level and dims the logger name."""

    def format(self, record: logging.LogRecord) -> str:
        colour = LEVEL_COLOR.get(record.levelno, "")
        original_level, original_name = record.levelname, record.name
        try:
            record.levelname = f"{colour}{original_level:<8}{RESET}"
            record.name = f"\033[2m{original_name:<16}{RESET}"
            return super().format(record)
        finally:
            record.levelname, record.name = original_level, original_name


def _supports_colour(stream) -> bool:
    if os.environ.get("NO_COLOR"):        # https://no-color.org
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())

_configured = False


def setup_logging(
    level: str = "INFO",
    *,
    path: Optional[Path] = None,
    extra_handler: Optional[logging.Handler] = None,
    force: bool = False,
) -> Path:
    """Configure root logging once.  Returns the log file path."""
    global _configured
    target = Path(path) if path else log_file()
    root = logging.getLogger()

    if _configured and not force:
        if extra_handler is not None:
            root.addHandler(extra_handler)
        return target

    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)

    target.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(root.level)   # own level, so the root can go lower
    root.addHandler(file_handler)

    # Only attach a console handler when there actually is a console.
    stream = sys.stderr if sys.stderr is not None else None
    if stream is not None:
        try:
            stream.write("")
            console = logging.StreamHandler(stream)
            if _supports_colour(stream):
                console.setFormatter(ColourFormatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                                     datefmt=DATEFMT))
            else:
                console.setFormatter(formatter)
            console.setLevel(root.level)
            root.addHandler(console)
        except Exception:  # pragma: no cover - pythonw edge cases
            pass

    if extra_handler is not None:
        extra_handler.setFormatter(formatter)
        root.addHandler(extra_handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    _configured = True
    return target
