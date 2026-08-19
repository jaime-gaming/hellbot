"""Entry point for the desktop launcher (and for the built .exe).

Shows errors in a message box instead of a console, because when packaged with
`--noconsole` (or started with `pythonw`) there is nowhere for text to go.
"""

from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path

# Make `hell` / `launcher` importable when double-clicked from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _popup(title: str, message: str) -> None:
    """Best-effort message box; falls back to stderr when there is no GUI."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # type: ignore[attr-defined]
        return
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
        return
    except Exception:
        pass
    print(f"{title}: {message}", file=sys.stderr)


def main() -> int:
    # pythonw / --noconsole give None for stdout+stderr: make them harmless.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    try:
        import tkinter  # noqa: F401
    except Exception:
        _popup(
            "Welcome to Hell — missing Tkinter",
            "Python was installed without Tkinter, so the control panel cannot open.\n\n"
            "Windows: reinstall Python from python.org and keep the 'tcl/tk and IDLE' option ticked.\n"
            "Linux: install the python3-tk package.\n\n"
            "You can still run the bot in a console with:  python bot.py",
        )
        return 1

    try:
        from launcher.gui import run_gui

        return run_gui()
    except Exception as exc:  # noqa: BLE001
        _popup(
            "Welcome to Hell — crash",
            f"The launcher hit an unexpected error:\n\n{type(exc).__name__}: {exc}\n\n"
            f"{traceback.format_exc()[-1500:]}",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
