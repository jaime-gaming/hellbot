"""Deployment artefacts: the container, the healthcheck and the Windows files.

The Docker image once shipped without `Announcements.py`, so the container
crashed on the very first import — nothing in the test suite could see it,
because nothing tested the *packaging*.  These tests build the file set each
deployment path declares and prove the bot can actually start from it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hell.models import EventStatus
from hell.storage import Store

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"


def copy_lines() -> list[tuple[list[str], str]]:
    """Every `COPY src... dest` in the Dockerfile."""
    out: list[tuple[list[str], str]] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^COPY\s+(.+)$", line.strip())
        if not match:
            continue
        parts = match.group(1).split()
        out.append((parts[:-1], parts[-1]))
    return out


# --------------------------------------------------------------- the image


def test_the_image_contains_everything_the_bot_imports(tmp_path):
    """Rebuild the container's /app from the Dockerfile and import the bot."""
    app = tmp_path / "app"
    app.mkdir()

    for sources, dest in copy_lines():
        target = app / dest.lstrip("./")
        for source in sources:
            origin = ROOT / source.rstrip("/")
            if not origin.exists():
                pytest.fail(f"Dockerfile copies {source!r}, which does not exist")
            if origin.is_dir():
                shutil.copytree(origin, target if dest.endswith("/") else target / origin.name,
                                dirs_exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True) if dest.endswith("/") else None
                destination = (target / origin.name) if dest.endswith("/") or target.is_dir() else target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)

    result = subprocess.run(
        [sys.executable, "-c", "import bot; import hell.texts as t; print(t.source())"],
        cwd=app,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"the container image cannot import the bot:\n{result.stderr}"
    assert "Announcements.py" in result.stdout


def test_the_image_runs_the_bot_and_the_healthcheck():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CMD ["python", "bot.py"]' in text
    assert "healthcheck.py" in text
    assert "VOLUME" in text and "/data" in text          # state survives redeploys
    assert "USER hellbot" in text                        # not running as root


def test_compose_and_service_point_at_real_files():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "env_file: .env" in compose
    assert "hell-data:/data" in compose

    service = (ROOT / "deploy" / "hellbot.service").read_text(encoding="utf-8")
    assert "ExecStart=" in service and "bot.py" in service
    assert "Restart=always" in service


# ---------------------------------------------------------- the healthcheck


def load_healthcheck():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hc", ROOT / "tools" / "healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def make_db(tmp_path, status: EventStatus, last_tick):
    store = Store(tmp_path / "hell.sqlite3")
    state = store.load_state()
    state.status = status
    state.event_uid = "uid"
    state.start_ts = time.time() - 3600
    state.last_tick_ts = last_tick
    store.save_state(state)
    store.close()
    return tmp_path / "hell.sqlite3"


def test_healthy_when_there_is_no_database(tmp_path):
    hc = load_healthcheck()
    healthy, reason = hc.check(tmp_path / "missing.sqlite3", 120)
    assert healthy and "no database" in reason


def test_healthy_while_ticking(tmp_path):
    hc = load_healthcheck()
    healthy, reason = hc.check(make_db(tmp_path, EventStatus.RUNNING, time.time() - 5), 120)
    assert healthy and "5s ago" in reason


def test_unhealthy_when_the_monitor_stalls(tmp_path):
    """A live process that stopped watching the VC must be restarted."""
    hc = load_healthcheck()
    healthy, reason = hc.check(make_db(tmp_path, EventStatus.RUNNING, time.time() - 600), 120)
    assert not healthy and "has not been observed" in reason


def test_healthy_when_no_event_is_running(tmp_path):
    hc = load_healthcheck()
    healthy, _ = hc.check(make_db(tmp_path, EventStatus.COMPLETED, time.time() - 99999), 120)
    assert healthy


def test_healthcheck_runs_as_a_script(tmp_path):
    database = make_db(tmp_path, EventStatus.RUNNING, time.time() - 600)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "healthcheck.py"), "--database", str(database)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "UNHEALTHY" in result.stdout


# ------------------------------------------------------------- the Windows files


def test_windows_launchers_reference_the_right_entry_points():
    bat = (ROOT / "run_bot.bat").read_text(encoding="utf-8", errors="replace")
    assert "launcher_main.py" in bat
    assert "pythonw.exe" in bat                          # no console window
    assert "requirements.txt" in bat                     # first-run install

    console = (ROOT / "run_bot_console.bat").read_text(encoding="utf-8", errors="replace")
    assert "bot.py" in console

    vbs = (ROOT / "run_bot_silent.vbs").read_text(encoding="utf-8", errors="replace")
    assert "run_bot.bat" in vbs

    build = (ROOT / "build_exe.bat").read_text(encoding="utf-8", errors="replace")
    assert "hellbot.spec" in build
    assert "Announcements.py" in build                   # editable next to the exe


def test_pyinstaller_spec_bundles_the_message_file_and_entry_point():
    spec = (ROOT / "hellbot.spec").read_text(encoding="utf-8")
    assert "launcher_main.py" in spec
    assert "Announcements" in spec
    assert "console=False" in spec                       # GUI build, no console
    for module in ("hell.texts", "hell.logsink", "hell.dm", "hell.grace"):
        assert module in spec, f"{module} missing from hiddenimports"


def test_the_build_context_excludes_secrets_and_state():
    """A stray .env or data/ in the image would ship credentials and history."""
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in (".env", "data/", "logs/", ".git/", "*.sqlite3", ".venv/"):
        assert entry in ignore, f".dockerignore should exclude {entry}"


# ------------------------------------------------------------------- branding

def test_the_logo_assets_are_present_and_usable():
    """The launcher window, the taskbar and the built .exe all need these."""
    master = ROOT / "assets" / "hellbotlogo.png"
    png = ROOT / "assets" / "hellbot.png"
    header_png = ROOT / "assets" / "hellbot-48.png"
    ico = ROOT / "assets" / "hellbot.ico"

    assert master.is_file(), "the master artwork is missing"
    for image in (master, png, header_png):
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{image.name} is not a PNG"
    assert ico.read_bytes()[:4] == b"\x00\x00\x01\x00", "hellbot.ico is not an ICO"

    # Windows shows the icon at many sizes; a single-resolution .ico looks bad.
    icon_count = int.from_bytes(ico.read_bytes()[4:6], "little")
    assert icon_count >= 4, f"hellbot.ico only contains {icon_count} size(s)"

    # The header image must be small: Tk downscaling looks ragged.
    assert header_png.stat().st_size < 20_000


def test_the_icons_can_be_rebuilt_from_the_master(tmp_path):
    """`python tools/make_icon.py` is the documented way to change the logo."""
    pytest.importorskip("PIL")
    import importlib.util

    spec = importlib.util.spec_from_file_location("mk", ROOT / "tools" / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module.MASTER.is_file()
    assert [path.name for path in module.build(module.MASTER)] == [
        "hellbot.png",
        "hellbot-48.png",
        "hellbot.ico",
    ]


def test_the_spec_and_readme_use_the_logo():
    spec = (ROOT / "hellbot.spec").read_text(encoding="utf-8")
    assert "hellbot.ico" in spec
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "assets/hellbot.png" in readme


def test_healthy_while_paused(tmp_path):
    """A paused event intentionally stops ticking; the healthcheck must not
    mistake the frozen `last_tick_ts` for a stalled monitor (which would make
    Docker/systemd kill the bot mid-pause)."""
    hc = load_healthcheck()
    store = Store(tmp_path / "hell.sqlite3")
    state = store.load_state()
    state.status = EventStatus.RUNNING
    state.event_uid = "uid"
    state.start_ts = time.time() - 3600
    state.last_tick_ts = time.time() - 9999      # very stale on purpose
    state.paused_ts = time.time() - 9990
    store.save_state(state)
    store.close()

    healthy, reason = hc.check(tmp_path / "hell.sqlite3", 120)
    assert healthy
    assert "PAUSED" in reason
