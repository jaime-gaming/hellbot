"""Logging setup: rotating file + console, safe when there is no console.

A GUI/`pythonw`/`--noconsole` process has `sys.stderr is None`; attaching a
StreamHandler to it raises at the first log record.  This module handles that,
which is exactly what the desktop launcher needs.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from .paths import log_file

FORMAT = "%(asctime)s %(levelname)-8s %(name)-16s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

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
    root.addHandler(file_handler)

    # Only attach a console handler when there actually is a console.
    stream = sys.stderr if sys.stderr is not None else None
    if stream is not None:
        try:
            stream.write("")
            console = logging.StreamHandler(stream)
            console.setFormatter(formatter)
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
