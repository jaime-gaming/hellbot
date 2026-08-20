"""The desktop launcher window, exercised headless through a fake tkinter.

Everything here runs the real `launcher/gui.py` logic; only the Tk widgets are
stubs (see `tests/faketk.py`).  Without this, the GUI was the one part of the
project no test ever executed.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from launcher import envfile
from launcher.runtime import ERROR, RUNNING, STARTING, STOPPED, Stats
from tests.faketk import install as install_faketk
from tests.test_launcher import GOOD


@pytest.fixture
def gui():
    """Import launcher.gui against the fake tkinter, fresh each time.

    sys.modules is restored afterwards so no other test ever sees the stubs.
    """
    stubbed = ("tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "launcher.gui")
    saved = {name: sys.modules.get(name) for name in stubbed}
    recorder = install_faketk()
    sys.modules.pop("launcher.gui", None)
    try:
        yield importlib.import_module("launcher.gui"), recorder
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class StubSupervisor:
    """Stands in for BotSupervisor without touching Discord or threads."""

    def __init__(self, env_file, *, status=STOPPED, stats=None):
        self.env_file = env_file
        self._status = status
        self._stats = stats or Stats(status=status)
        self.started = 0
        self.stopped = 0
        self.log_handler = _Handler()
        self.start_error: Exception | None = None

    @property
    def is_running(self):
        return self._status in (STARTING, RUNNING)

    @property
    def status(self):
        return self._status

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started += 1
        self._status = RUNNING
        self._stats.status = RUNNING

    def stop(self, timeout=None):
        self.stopped += 1
        self._status = STOPPED
        self._stats.status = STOPPED

    def stats(self):
        return self._stats

    def log_file_path(self):
        return self.env_file.parent / "hellbot.log"


class _Handler:
    def __init__(self):
        self.lines: list[str] = []

    def drain(self, limit=500):
        out, self.lines = self.lines[:limit], self.lines[limit:]
        return out


def make_app(gui_module, tmp_path, **kwargs):
    env = tmp_path / ".env"
    values = envfile.read_env(env)
    values.update(GOOD)
    envfile.write_env(env, values)
    supervisor = StubSupervisor(env, **kwargs)
    return gui_module.LauncherApp(supervisor), supervisor


# ------------------------------------------------------------------ building

def test_window_builds_with_every_tab_and_control(gui, tmp_path):
    module, _recorder = gui
    app, _sup = make_app(module, tmp_path)

    assert app.title_text.startswith("Welcome to Hell")
    assert [text for _tab, text in app.nb.tabs] == ["Dashboard", "Log", "Settings"]
    assert "WM_DELETE_WINDOW" in app.protocols          # closing is handled
    assert app.after_calls                               # the poll loop is scheduled
    # every configurable setting has an input bound to it
    assert set(app.vars) == {field.key for field in envfile.FIELDS}


def test_settings_are_loaded_from_the_env_file(gui, tmp_path):
    module, _recorder = gui
    app, _sup = make_app(module, tmp_path)
    assert app.vars["GUILD_ID"].get() == GOOD["GUILD_ID"]
    assert app.vars["DISCORD_TOKEN"].get() == GOOD["DISCORD_TOKEN"]


# ------------------------------------------------------------------ starting

def test_start_writes_the_config_and_starts_the_bot(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path)
    app.vars["ANNOUNCE_CHANNEL_ID"].set("777")

    app.on_start()

    assert sup.started == 1
    assert envfile.read_env(sup.env_file)["ANNOUNCE_CHANNEL_ID"] == "777"
    assert "showerror" not in recorder.kinds()


def test_incomplete_config_blocks_start_and_opens_settings(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path)
    app.vars["DISCORD_TOKEN"].set("")

    app.on_start()

    assert sup.started == 0
    assert recorder.kinds() == ["showerror"]
    assert app.nb.selected is app.tab_cfg          # the user is shown where to fix it


def test_a_config_error_from_the_supervisor_is_shown(gui, tmp_path):
    from hell.config import ConfigError

    module, recorder = gui
    app, sup = make_app(module, tmp_path)
    sup.start_error = ConfigError("bad ID")

    app.on_start()

    assert ("showerror", "Configuration error", "bad ID") in recorder.calls


# ------------------------------------------------------------------ stopping

def test_stop_asks_for_confirmation(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path, status=RUNNING)

    recorder.answer = False
    app.on_stop()
    assert sup.stopped == 0                        # declined -> nothing happens

    recorder.answer = True
    app.on_stop()
    assert sup.stopped == 1
    assert "askyesno" in recorder.kinds()


def test_closing_while_running_confirms_then_stops(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path, status=RUNNING)

    recorder.answer = False
    app.on_close()
    assert not app.destroyed and sup.stopped == 0

    recorder.answer = True
    app.on_close()
    assert app.destroyed and sup.stopped == 1


def test_closing_while_idle_does_not_nag(gui, tmp_path):
    module, recorder = gui
    app, _sup = make_app(module, tmp_path)
    app.on_close()
    assert app.destroyed
    assert recorder.calls == []


# ------------------------------------------------------------------ dashboard

def test_dashboard_reflects_a_running_event(gui, tmp_path):
    module, _recorder = gui
    stats = Stats(
        status=RUNNING,
        connected_as="HellBot#0001",
        event_status="RUNNING",
        elapsed=73 * 3600 + 24 * 60,
        fraction=0.459,
        remaining=86 * 3600 + 36 * 60,
        participants=7,
        next_milestone="96h",
        time_to_next="22h 36m",
        alive_check="🚨 Alive checks: random",
        uptime=3600,
    )
    app, _sup = make_app(module, tmp_path, status=RUNNING, stats=stats)

    app._refresh()

    assert app.lbl_event.kwargs["text"] == "RUNNING"
    assert app.lbl_people.kwargs["text"] == "7"
    assert app.lbl_next.kwargs["text"] == "96h"
    assert "73h 24m / 160h 00m" in app.lbl_progress.kwargs["text"]
    assert "45.9%" in app.lbl_progress.kwargs["text"]
    assert app.pb.kwargs["value"] == pytest.approx(45.9, abs=0.1)
    assert "HellBot#0001" in app.lbl_status.kwargs["text"]
    assert app.btn_start.kwargs["state"] == "disabled"
    assert app.btn_stop.kwargs["state"] == "normal"


def test_dashboard_shows_health_problems(gui, tmp_path):
    module, _recorder = gui
    stats = Stats(
        status=ERROR,
        detail="Discord rejected the token.",
        health_errors=["Missing 'Move Members' on 'hell'"],
        health_warnings=["Missing 'Mention @everyone'"],
    )
    app, _sup = make_app(module, tmp_path, stats=stats)

    app._refresh()

    text = app.txt_health.content
    assert "Discord rejected the token." in text
    assert "Move Members" in text
    assert "Mention @everyone" in text


def test_idle_dashboard_explains_what_to_do(gui, tmp_path):
    module, _recorder = gui
    app, _sup = make_app(module, tmp_path)
    app._refresh()
    assert "/hell start" in app.lbl_event_sub.kwargs["text"]


# ------------------------------------------------------------------ log pane

def test_log_lines_are_pumped_into_the_view(gui, tmp_path):
    module, _recorder = gui
    app, sup = make_app(module, tmp_path)
    sup.log_handler.lines = [
        "12:00:01 INFO     hell.monitor: Alice joined the VC",
        "12:00:02 ERROR    hell.bot: something broke",
    ]

    app._pump_logs()

    assert "Alice joined the VC" in app.txt_log.content
    assert "something broke" in app.txt_log.content
    assert {"ERROR", "WARNING", "INFO", "DEBUG"} <= set(app.txt_log.tags)


def test_clear_view_empties_the_log_pane(gui, tmp_path):
    module, _recorder = gui
    app, sup = make_app(module, tmp_path)
    sup.log_handler.lines = ["12:00:01 INFO hell: hello"]
    app._pump_logs()
    app.on_clear_log()
    assert app.txt_log.content == ""


def test_tick_pumps_and_refreshes_then_reschedules(gui, tmp_path):
    module, _recorder = gui
    app, sup = make_app(module, tmp_path)
    sup.log_handler.lines = ["12:00:03 INFO hell: tick"]
    before = len(app.after_calls)

    app._tick()

    assert "tick" in app.txt_log.content
    assert len(app.after_calls) == before + 1      # keeps polling


# ------------------------------------------------------------------ settings

def test_save_writes_and_offers_a_restart_when_running(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path, status=RUNNING)
    app.vars["HEARTBEAT_MINUTES"].set("30")
    recorder.answer = True

    app.on_save()

    assert envfile.read_env(sup.env_file)["HEARTBEAT_MINUTES"] == "30"
    assert sup.stopped == 1 and sup.started == 1   # restarted on request


def test_save_with_problems_asks_before_writing(gui, tmp_path):
    module, recorder = gui
    app, sup = make_app(module, tmp_path)
    app.vars["GUILD_ID"].set("not-a-number")
    recorder.answer = False

    app.on_save()

    assert envfile.read_env(sup.env_file)["GUILD_ID"] == GOOD["GUILD_ID"]  # untouched
    assert recorder.kinds() == ["askyesno"]


def test_reload_restores_values_from_disk(gui, tmp_path):
    module, _recorder = gui
    app, _sup = make_app(module, tmp_path)
    app.vars["GUILD_ID"].set("999")

    app.on_reload()

    assert app.vars["GUILD_ID"].get() == GOOD["GUILD_ID"]


def test_check_configuration_reports_success_and_problems(gui, tmp_path):
    module, recorder = gui
    app, _sup = make_app(module, tmp_path)

    app.on_check()
    assert recorder.kinds() == ["showinfo"]

    recorder.clear()
    app.vars["CLANKER_ROLE_ID"].set("")
    app.on_check()
    assert recorder.kinds() == ["showwarning"]


def test_first_run_without_a_config_opens_settings(gui, tmp_path):
    module, recorder = gui
    supervisor = StubSupervisor(tmp_path / "missing.env")
    app = module.LauncherApp(supervisor)

    app._first_run_check()

    assert app.nb.selected is app.tab_cfg
    assert "First run" in recorder.titles()


def test_header_shows_the_logo_and_version(gui, tmp_path):
    """The window is the product's face: logo, title, version."""
    from hell import __version__

    module, _recorder = gui
    app, _sup = make_app(module, tmp_path)

    labels = _all_text(app)
    assert any("WELCOME TO HELL" in text for text in labels)
    assert any(__version__ in text for text in labels)
    assert getattr(app, "_logo_image", None) is not None


def test_palette_is_a_complete_set_of_hex_colours(gui):
    """A missing/typo'd colour is a crash on a real Tk, not a wrong shade."""
    import re

    module, _recorder = gui
    for name in ("BG", "CARD", "CARD_HI", "FG", "MUTED", "ACCENT", "ACCENT_DARK", "OK", "WARN", "BAD"):
        value = getattr(module, name)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{name}={value!r} is not a hex colour"


def _all_text(widget, found=None):
    found = [] if found is None else found
    text = widget.kwargs.get("text") if hasattr(widget, "kwargs") else None
    if isinstance(text, str):
        found.append(text)
    for child in getattr(widget, "children", []):
        _all_text(child, found)
    return found
