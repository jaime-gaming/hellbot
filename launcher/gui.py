"""Desktop control panel (tkinter) — no console needed.

Three tabs:
  * **Dashboard** — bot state, event state, live progress bar, health warnings.
  * **Log** — the same lines that go to `logs/hellbot.log`, live.
  * **Settings** — every `.env` value with inline help; saved atomically.

The bot itself runs in a background thread (see `launcher.runtime`), so the
window never freezes and closing it shuts the bot down cleanly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from hell import __version__
from hell.config import ConfigError
from hell.paths import log_file
from hell.timeutil import format_hm

from . import envfile
from .runtime import ERROR, RUNNING, STARTING, STOPPED, STOPPING, BotSupervisor

# Palette — a dark, ember-lit theme that matches the bot's embeds.
BG = "#131419"
CARD = "#1c1e26"
CARD_HI = "#242733"
FG = "#eceaf0"
MUTED = "#8b909c"
ACCENT = "#e25822"
ACCENT_DARK = "#b8420f"
OK = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"

FONT = "Segoe UI"
MONO = "Consolas"

STATUS_COLORS = {
    STOPPED: MUTED,
    STARTING: WARN,
    RUNNING: OK,
    STOPPING: WARN,
    ERROR: BAD,
}

INVITE_HELP = "https://discord.com/developers/applications"


class LauncherApp(tk.Tk):
    def __init__(self, supervisor: Optional[BotSupervisor] = None):
        super().__init__()
        self.supervisor = supervisor or BotSupervisor()
        self.values = envfile.read_env(self.supervisor.env_file)
        self.vars: dict[str, tk.StringVar] = {}

        self.title("Welcome to Hell — Bot Control")
        self.geometry("980x660")
        self.minsize(860, 580)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_icon()

        self._build_style()
        self._build_header()
        self._build_tabs()
        self._build_statusbar()

        self.after(400, self._tick)
        self.after(600, self._first_run_check)

    def _load_logo(self):
        """A small version of the app icon for the header (optional)."""
        png = Path(__file__).resolve().parent.parent / "assets" / "hellbot.png"
        if not png.exists():
            return None
        try:
            image = tk.PhotoImage(file=str(png))
            factor = max(1, image.width() // 48)
            return image.subsample(factor, factor)
        except Exception:  # pragma: no cover - purely cosmetic
            return None

    def _set_icon(self) -> None:
        """Window/taskbar icon; silently ignored if the assets are missing."""
        base = Path(__file__).resolve().parent.parent
        ico, png = base / "assets" / "hellbot.ico", base / "assets" / "hellbot.png"
        try:
            if sys.platform.startswith("win") and ico.exists():
                self.iconbitmap(default=str(ico))
                return
            if png.exists():
                self._icon_image = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_image)
        except Exception:  # pragma: no cover - purely cosmetic
            pass

    # ------------------------------------------------------------- styling

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=CARD, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=FG, font=(FONT, 10))
        style.configure("Card.TLabel", background=CARD, foreground=FG, font=(FONT, 10))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("Caption.TLabel", background=CARD, foreground=MUTED, font=(FONT, 8))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 17, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("Big.TLabel", background=CARD, foreground=FG, font=(FONT, 22, "bold"))
        style.configure("Metric.TLabel", background=CARD, foreground=ACCENT, font=(FONT, 22, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 6, 2, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                        padding=(18, 9), font=(FONT, 10))
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", FG), ("active", FG)],
        )
        style.configure("TButton", background=CARD_HI, foreground=FG, padding=(14, 8),
                        font=(FONT, 10), borderwidth=0)
        style.map("TButton", background=[("active", "#2f3342"), ("disabled", CARD)],
                  foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        padding=(18, 9), font=(FONT, 10, "bold"), borderwidth=0)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#5c4034")],
                  foreground=[("disabled", "#c9c4c0")])
        style.configure("TEntry", fieldbackground=CARD_HI, foreground=FG, insertcolor=FG,
                        padding=7, borderwidth=0)
        style.configure("TCheckbutton", background=CARD, foreground=FG, font=(FONT, 9))
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Hell.Horizontal.TProgressbar", troughcolor=CARD_HI, background=ACCENT,
                        thickness=16, borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TScrollbar", background=CARD_HI, troughcolor=BG, borderwidth=0,
                        arrowcolor=MUTED)

    # -------------------------------------------------------------- header

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(20, 16, 20, 10))
        header.pack(fill="x")

        logo = self._load_logo()
        if logo is not None:
            self._logo_image = logo
            tk.Label(header, image=logo, bg=BG, borderwidth=0).pack(side="left", padx=(0, 14))

        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        ttk.Label(title_box, text="🔥  WELCOME TO HELL", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text=f"160-hour voice channel challenge · bot v{__version__}",
            style="Sub.TLabel",
        ).pack(anchor="w")

        self.btn_start = ttk.Button(header, text="▶  Start bot", style="Accent.TButton", command=self.on_start)
        self.btn_start.pack(side="right", padx=(8, 0))
        self.btn_stop = ttk.Button(header, text="■  Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="right", padx=(8, 0))

        self.status_dot = tk.Canvas(header, width=14, height=14, bg=BG, highlightthickness=0)
        self.status_dot.pack(side="right", padx=(16, 6))
        self._dot = self.status_dot.create_oval(2, 2, 12, 12, fill=MUTED, outline="")
        self.lbl_status = ttk.Label(header, text="Stopped", foreground=MUTED)
        self.lbl_status.pack(side="right")

    # ---------------------------------------------------------------- tabs

    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 6))
        self.tab_dash = ttk.Frame(self.nb, padding=14)
        self.tab_log = ttk.Frame(self.nb, padding=14)
        self.tab_cfg = ttk.Frame(self.nb, padding=14)
        self.nb.add(self.tab_dash, text="Dashboard")
        self.nb.add(self.tab_log, text="Log")
        self.nb.add(self.tab_cfg, text="Settings")
        self._build_dashboard()
        self._build_log()
        self._build_settings()

    def _card(self, parent, title: str) -> ttk.Frame:
        outer = ttk.Frame(parent, style="Card.TFrame", padding=16)
        ttk.Label(outer, text=title.upper(), style="Caption.TLabel").pack(anchor="w")
        return outer

    def _build_dashboard(self) -> None:
        top = ttk.Frame(self.tab_dash)
        top.pack(fill="x")

        self.card_event = self._card(top, "Event status")
        self.card_event.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.lbl_event = ttk.Label(self.card_event, text="IDLE", style="Big.TLabel")
        self.lbl_event.pack(anchor="w", pady=(6, 0))
        self.lbl_event_sub = ttk.Label(self.card_event, text="No event has been started.", style="Muted.TLabel")
        self.lbl_event_sub.pack(anchor="w")

        self.card_vc = self._card(top, "In the VC")
        self.card_vc.pack(side="left", fill="both", expand=True, padx=8)
        self.lbl_people = ttk.Label(self.card_vc, text="0", style="Metric.TLabel")
        self.lbl_people.pack(anchor="w", pady=(6, 0))
        self.lbl_people_sub = ttk.Label(self.card_vc, text="valid humans", style="Muted.TLabel")
        self.lbl_people_sub.pack(anchor="w")

        self.card_next = self._card(top, "Next milestone")
        self.card_next.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.lbl_next = ttk.Label(self.card_next, text="—", style="Metric.TLabel")
        self.lbl_next.pack(anchor="w", pady=(6, 0))
        self.lbl_next_sub = ttk.Label(self.card_next, text="", style="Muted.TLabel")
        self.lbl_next_sub.pack(anchor="w")

        progress = ttk.Frame(self.tab_dash, style="Card.TFrame", padding=16)
        progress.pack(fill="x", pady=14)
        ttk.Label(progress, text="PROGRESS", style="Caption.TLabel").pack(anchor="w")
        self.pb = ttk.Progressbar(progress, style="Hell.Horizontal.TProgressbar", maximum=100.0)
        self.pb.pack(fill="x", pady=8)
        self.lbl_progress = ttk.Label(progress, text="0h 00m / 160h 00m — 0.0%", style="Card.TLabel")
        self.lbl_progress.pack(anchor="w")
        self.lbl_alive = ttk.Label(progress, text="", style="Muted.TLabel")
        self.lbl_alive.pack(anchor="w", pady=(4, 0))

        health = ttk.Frame(self.tab_dash, style="Card.TFrame", padding=16)
        health.pack(fill="both", expand=True)
        ttk.Label(health, text="CHECKS", style="Caption.TLabel").pack(anchor="w")
        self.txt_health = tk.Text(
            health, height=8, bg=CARD, fg=FG, relief="flat", wrap="word",
            insertbackground=FG, highlightthickness=0, font=(FONT, 9), padx=2, pady=4,
        )
        self.txt_health.pack(fill="both", expand=True, pady=(8, 0))
        self.txt_health.insert("1.0", "Start the bot to run the configuration checks.")
        self.txt_health.configure(state="disabled")

    def _build_log(self) -> None:
        bar = ttk.Frame(self.tab_log)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Open log folder", command=self.on_open_logs).pack(side="left")
        ttk.Button(bar, text="Clear view", command=self.on_clear_log).pack(side="left", padx=8)
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.var_autoscroll).pack(side="left", padx=8)
        ttk.Label(bar, text=str(log_file()), style="Muted.TLabel", foreground=MUTED).pack(side="right")

        wrapper = ttk.Frame(self.tab_log, style="Card.TFrame")
        wrapper.pack(fill="both", expand=True)
        self.txt_log = tk.Text(
            wrapper, bg="#0d0e12", fg="#cfd3da", relief="flat", wrap="none",
            insertbackground=FG, highlightthickness=0, font=(MONO, 9), padx=10, pady=8,
        )
        scroll = ttk.Scrollbar(wrapper, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ERROR", BAD), ("WARNING", WARN), ("INFO", "#8ab4f8"), ("DEBUG", MUTED)):
            self.txt_log.tag_configure(tag, foreground=color)

    def _build_settings(self) -> None:
        intro = ttk.Frame(self.tab_cfg)
        intro.pack(fill="x")
        ttk.Label(
            intro,
            text="Enable Developer Mode in Discord (Settings → Advanced) to copy IDs by right-clicking.",
            style="Muted.TLabel",
            foreground=MUTED,
        ).pack(side="left")
        ttk.Button(intro, text="Developer portal", command=lambda: webbrowser.open(INVITE_HELP)).pack(side="right")

        canvas = tk.Canvas(self.tab_cfg, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(self.tab_cfg, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, pady=10)
        scroll.pack(side="right", fill="y", pady=10)

        for section in envfile.SECTIONS:
            block = ttk.Frame(inner, style="Card.TFrame", padding=12)
            block.pack(fill="x", pady=6, padx=(0, 12))
            ttk.Label(block, text=section.upper(), style="Muted.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
            )
            for row, fld in enumerate(envfile.fields_for(section), start=1):
                label = fld.label + (" *" if fld.required else "")
                ttk.Label(block, text=label, style="Card.TLabel", width=32).grid(
                    row=row, column=0, sticky="w", pady=3
                )
                var = tk.StringVar(value=self.values.get(fld.key, fld.default))
                self.vars[fld.key] = var
                entry = ttk.Entry(block, textvariable=var, width=46, show="•" if fld.secret else "")
                entry.grid(row=row, column=1, sticky="we", pady=3)
                if fld.secret:
                    shown = tk.BooleanVar(value=False)

                    def toggle(e=entry, v=shown):
                        e.configure(show="" if v.get() else "•")

                    ttk.Checkbutton(block, text="show", variable=shown, command=toggle).grid(
                        row=row, column=2, sticky="w", padx=6
                    )
                elif fld.help:
                    ttk.Label(block, text=fld.help, style="Muted.TLabel", wraplength=320).grid(
                        row=row, column=2, sticky="w", padx=6
                    )
                block.grid_columnconfigure(1, weight=1)

        buttons = ttk.Frame(self.tab_cfg)
        buttons.pack(fill="x", side="bottom", pady=(6, 0))
        ttk.Button(buttons, text="💾  Save settings", style="Accent.TButton", command=self.on_save).pack(side="left")
        ttk.Button(buttons, text="Reload from file", command=self.on_reload).pack(side="left", padx=8)
        ttk.Button(buttons, text="Check configuration", command=self.on_check).pack(side="left")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(18, 6))
        bar.pack(fill="x", side="bottom")
        self.lbl_footer = ttk.Label(bar, text=f"Config: {self.supervisor.env_file}", style="Muted.TLabel",
                                    foreground=MUTED)
        self.lbl_footer.pack(side="left")
        self.lbl_uptime = ttk.Label(bar, text="", style="Muted.TLabel", foreground=MUTED)
        self.lbl_uptime.pack(side="right")

    # ------------------------------------------------------------- actions

    def on_start(self) -> None:
        self._collect_values()
        problems = envfile.validate(self.values)
        if problems:
            self.nb.select(self.tab_cfg)
            messagebox.showerror("Configuration incomplete", "\n".join(problems), parent=self)
            return
        envfile.write_env(self.supervisor.env_file, self.values)
        try:
            self.supervisor.start()
        except ConfigError as exc:
            messagebox.showerror("Configuration error", str(exc), parent=self)
        except RuntimeError as exc:
            messagebox.showwarning("Already running", str(exc), parent=self)
        except Exception as exc:
            messagebox.showerror("Could not start", f"{type(exc).__name__}: {exc}", parent=self)

    def on_stop(self) -> None:
        if not self.supervisor.is_running:
            return
        if not messagebox.askyesno(
            "Stop the bot?",
            "The event timer is based on timestamps, so stopping the bot does NOT reset it — "
            "but while the bot is offline it cannot watch the VC or post milestones.\n\nStop now?",
            parent=self,
        ):
            return
        self.btn_stop.configure(state="disabled")
        self.supervisor.stop()

    def on_save(self) -> None:
        self._collect_values()
        problems = envfile.validate(self.values)
        if problems and not messagebox.askyesno(
            "Save anyway?", "\n".join(problems) + "\n\nSave the file regardless?", parent=self
        ):
            return
        path = envfile.write_env(self.supervisor.env_file, self.values)
        if self.supervisor.is_running and messagebox.askyesno(
            "Restart the bot?", f"Saved to {path}.\n\nRestart the bot so the changes take effect?", parent=self
        ):
            self.supervisor.stop()
            self.supervisor.start()
        else:
            messagebox.showinfo("Saved", f"Configuration written to:\n{path}", parent=self)

    def on_reload(self) -> None:
        self.values = envfile.read_env(self.supervisor.env_file)
        for key, var in self.vars.items():
            var.set(self.values.get(key, envfile.BY_KEY[key].default if key in envfile.BY_KEY else ""))
        messagebox.showinfo("Reloaded", "Settings reloaded from disk.", parent=self)

    def on_check(self) -> None:
        self._collect_values()
        problems = envfile.validate(self.values)
        if problems:
            messagebox.showwarning("Configuration problems", "\n".join(problems), parent=self)
            return
        stats = self.supervisor.stats()
        if stats.health_errors or stats.health_warnings:
            messagebox.showwarning(
                "Discord checks",
                "\n".join(["ERRORS:", *stats.health_errors, "", "WARNINGS:", *stats.health_warnings]),
                parent=self,
            )
            return
        messagebox.showinfo(
            "Looks good",
            "Every required value is filled in."
            + ("\nDiscord-side checks passed too." if self.supervisor.is_running else
               "\nStart the bot to run the Discord-side checks."),
            parent=self,
        )

    def on_open_logs(self) -> None:
        folder = Path(self.supervisor.log_file_path()).parent
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showinfo("Log folder", f"{folder}\n\n({exc})", parent=self)

    def on_clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def on_close(self) -> None:
        if self.supervisor.is_running:
            if not messagebox.askyesno(
                "Quit?",
                "The bot is running. Closing this window stops it — the VC will no longer be watched.\n\nQuit?",
                parent=self,
            ):
                return
            self.supervisor.stop(timeout=15)
        self.destroy()

    # -------------------------------------------------------------- polling

    def _collect_values(self) -> None:
        for key, var in self.vars.items():
            self.values[key] = var.get().strip()

    def _first_run_check(self) -> None:
        if Path(self.supervisor.env_file).exists():
            return
        self.nb.select(self.tab_cfg)
        messagebox.showinfo(
            "First run",
            "No .env file found, so let's set one up.\n\n"
            "Fill in the token, the server ID, the two channel IDs and the two role IDs, "
            "press “Save settings”, then “Start bot”.",
            parent=self,
        )

    def _tick(self) -> None:
        try:
            self._pump_logs()
            self._refresh()
        finally:
            self.after(500, self._tick)

    def _pump_logs(self) -> None:
        lines = self.supervisor.log_handler.drain()
        if not lines:
            return
        self.txt_log.configure(state="normal")
        for line in lines:
            tag = ""
            for level in ("ERROR", "WARNING", "INFO", "DEBUG"):
                if f" {level} " in line or f" {level:<8}" in line:
                    tag = level
                    break
            self.txt_log.insert("end", line + "\n", tag)
        # keep the widget bounded
        if int(self.txt_log.index("end-1c").split(".")[0]) > 4000:
            self.txt_log.delete("1.0", "2000.0")
        self.txt_log.configure(state="disabled")
        if self.var_autoscroll.get():
            self.txt_log.see("end")

    def _refresh(self) -> None:
        stats = self.supervisor.stats()

        color = STATUS_COLORS.get(stats.status, MUTED)
        self.status_dot.itemconfigure(self._dot, fill=color)
        label = {STOPPED: "Stopped", STARTING: "Starting…", RUNNING: "Running",
                 STOPPING: "Stopping…", ERROR: "Error"}.get(stats.status, stats.status)
        if stats.connected_as and stats.status == RUNNING:
            label = f"Running as {stats.connected_as}"
        self.lbl_status.configure(text=label, foreground=color)

        running = stats.status in (STARTING, RUNNING, STOPPING)
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

        self.lbl_event.configure(text=stats.event_status)
        subtitle = {
            "IDLE": "No event yet — a @gamenight host runs /hell start.",
            "RUNNING": "The VC must never be empty.",
            "FAILED": "The VC emptied. The timer is stopped permanently.",
            "COMPLETED": "160 consecutive hours survived.",
            "CANCELLED": "Manually stopped by a host.",
        }.get(stats.event_status, "")
        self.lbl_event_sub.configure(text=subtitle)

        self.lbl_people.configure(text=str(stats.participants))
        self.lbl_next.configure(text=stats.next_milestone)
        self.lbl_next_sub.configure(
            text=f"in {stats.time_to_next}" if stats.time_to_next != "—" else "all milestones cleared"
        )
        self.pb.configure(value=min(100.0, stats.fraction * 100.0))
        self.lbl_progress.configure(
            text=f"{stats.elapsed_text} — {stats.percent_text} — {format_hm(stats.remaining)} remaining"
        )

        self.lbl_alive.configure(text=stats.alive_check.replace("**", ""))

        if stats.uptime:
            self.lbl_uptime.configure(text=f"Bot uptime: {format_hm(stats.uptime)}   ")
        else:
            self.lbl_uptime.configure(text=stats.detail)

        self._render_health(stats)

    def _render_health(self, stats) -> None:
        lines: list[str] = []
        if stats.status == ERROR and stats.detail:
            lines.append(f"✖  {stats.detail}")
        for item in stats.health_errors:
            lines.append(f"✖  {item}")
        for item in stats.health_warnings:
            lines.append(f"⚠  {item}")
        if not lines:
            lines.append(
                "✔  No problems detected."
                if stats.status == RUNNING
                else "Start the bot to run the configuration checks."
            )
        text = "\n".join(lines)
        if getattr(self, "_health_text", None) == text:
            return
        self._health_text = text
        self.txt_health.configure(state="normal")
        self.txt_health.delete("1.0", "end")
        self.txt_health.insert("1.0", text)
        self.txt_health.configure(state="disabled")

    # ------------------------------------------------------------- errors

    def report_callback_exception(self, exc, val, tb) -> None:  # pragma: no cover - UI safety net
        details = "".join(traceback.format_exception(exc, val, tb))
        try:
            messagebox.showerror("Unexpected error", f"{val}\n\n{details[-1200:]}", parent=self)
        except Exception:
            pass


def run_gui() -> int:
    app = LauncherApp()
    app.mainloop()
    return 0
