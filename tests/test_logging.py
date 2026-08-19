"""Logging must work in a GUI/`pythonw` process, where there is no console."""

from __future__ import annotations

import logging
import sys

from hell import logging_setup


def _reset():
    logging_setup._configured = False
    for handler in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(handler)


def test_logging_writes_to_a_rotating_file(tmp_path):
    _reset()
    path = tmp_path / "logs" / "hellbot.log"
    logging_setup.setup_logging("INFO", path=path, force=True)
    logging.getLogger("hell.test").info("hello from the pit")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert path.exists()
    assert "hello from the pit" in path.read_text(encoding="utf-8")
    _reset()


def test_logging_survives_a_missing_console(tmp_path, monkeypatch):
    """pythonw / --noconsole set sys.stderr to None; a StreamHandler would crash."""
    _reset()
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)
    path = tmp_path / "hellbot.log"
    logging_setup.setup_logging("DEBUG", path=path, force=True)
    handlers = logging.getLogger().handlers
    assert all(not isinstance(h, logging.StreamHandler) or h.stream is not None for h in handlers)
    logging.getLogger("hell.test").warning("no console here")
    for handler in handlers:
        handler.flush()
    assert "no console here" in path.read_text(encoding="utf-8")
    _reset()


def test_extra_handler_is_attached(tmp_path):
    _reset()
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    logging_setup.setup_logging("INFO", path=tmp_path / "x.log", extra_handler=Capture(), force=True)
    logging.getLogger("hell.test").info("captured")
    assert "captured" in seen
    _reset()
