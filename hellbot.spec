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

a = Analysis(
    ["launcher_main.py"],
    pathex=[str(here)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "Announcements",
        "hell",
        "hell.bot",
        "hell.announcer",
        "hell.cog",
        "hell.engine",
        "hell.health",
        "hell.monitor",
        "hell.storage",
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
