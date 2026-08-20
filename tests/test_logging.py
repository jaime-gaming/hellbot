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


def test_console_output_is_coloured_only_on_a_terminal(tmp_path, monkeypatch):
    """Colour helps in a console; escape codes in a redirected file do not."""
    from hell.logging_setup import ColourFormatter, _supports_colour

    class Tty:
        def isatty(self):
            return True

        def write(self, _text):
            return None

    class Piped(Tty):
        def isatty(self):
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert _supports_colour(Tty()) is True
    assert _supports_colour(Piped()) is False

    monkeypatch.setenv("NO_COLOR", "1")
    assert _supports_colour(Tty()) is False       # honours the NO_COLOR convention

    record = logging.LogRecord("hell.monitor", logging.WARNING, "x.py", 1, "careful", None, None)
    rendered = ColourFormatter("%(levelname)s %(name)s %(message)s").format(record)
    assert "\033[33m" in rendered and "careful" in rendered
    # the record itself must be left untouched for the other handlers
    assert record.levelname == "WARNING" and record.name == "hell.monitor"


def test_the_log_file_never_contains_escape_codes(tmp_path, monkeypatch):
    _reset()
    path = tmp_path / "hellbot.log"
    monkeypatch.setenv("FORCE_COLOR", "1")
    logging_setup.setup_logging("INFO", path=path, force=True)
    logging.getLogger("hell.test").warning("plain please")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "\033[" not in path.read_text(encoding="utf-8")
    _reset()
