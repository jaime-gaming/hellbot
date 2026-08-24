# PyInstaller spec — builds a single-file, console-less Windows executable.
#
#   build_exe.bat            (or)      pyinstaller hellbot.spec
#
# Output: dist/WelcomeToHellBot.exe
# The exe keeps .env, data/ and logs/ next to itself (see hell/paths.py),
# so the whole thing can live in one folder and be copied around.

from pathlib import Path

block_cipher = None
here = Path(SPECPATH)

icon = here / "assets" / "hellbot.ico"
icon_arg = str(icon) if icon.exists() else None

datas = []
for extra in (".env.example", "Announcements.py"):
    path = here / extra
    if path.exists():
        datas.append((str(path), "."))

for folder in ("assets", "docs"):
    folder_path = here / folder
    if folder_path.exists():
        datas.append((str(folder_path), folder))

a = Analysis(
    ["launcher_main.py"],
    pathex=[str(here)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "Announcements",
        "hell",
        "hell.alivecheck",
        "hell.aliveio",
        "hell.announcer",
        "hell.assets",
        "hell.bot",
        "hell.cog",
        "hell.config",
        "hell.dm",
        "hell.embeds",
        "hell.engine",
        "hell.errorcodes",
        "hell.grace",
        "hell.health",
        "hell.leaderboard",
        "hell.logging_setup",
        "hell.logsink",
        "hell.milestones",
        "hell.models",
        "hell.monitor",
        "hell.pages_sync",
        "hell.paths",
        "hell.reports",
        "hell.security",
        "hell.status_writer",
        "hell.storage",
        "hell.tasks",
        "hell.texts",
        "hell.timeline",
        "hell.timeutil",
        "hell.tracking",
        "hell.ui",
        "hell.web",
        "launcher",
        "launcher.gui",
        "launcher.runtime",
        "launcher.envfile",
        "discord",
        "aiohttp",
        "dotenv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "matplotlib", "numpy", "PIL"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="WelcomeToHellBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # <- no console window, GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)
