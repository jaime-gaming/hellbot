"""Regenerate the app icons from the master logo.

    python tools/make_icon.py            # uses assets/hellbotlogo.png
    python tools/make_icon.py path.png   # or any other source

`assets/hellbotlogo.png` is the master artwork. Everything the app displays is
derived from it here, so updating the logo is: drop in a new master, run this,
commit. Requires Pillow (a dev dependency — the bot itself needs no imaging).

Derived files
    assets/hellbot.png     512x512, used by the README and as a general icon
    assets/hellbot-48.png  crisp 48x48 for the launcher header (Tk's own
                           downscaling is nearest-neighbour and looks rough)
    assets/hellbot.ico     multi-size Windows icon for the window and the .exe
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
MASTER = ASSETS / "hellbotlogo.png"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def build(source: Path) -> list[Path]:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - dev-only tool
        print("Pillow is required: pip install -r requirements-dev.txt", file=sys.stderr)
        raise SystemExit(2) from None

    if not source.is_file():
        print(f"Master logo not found: {source}", file=sys.stderr)
        raise SystemExit(1)

    image = Image.open(source).convert("RGBA")
    if image.width != image.height:  # keep icons square, centred on the artwork
        side = min(image.width, image.height)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side))

    written: list[Path] = []

    big = ASSETS / "hellbot.png"
    image.resize((512, 512), Image.LANCZOS).convert("RGB").save(big, optimize=True)
    written.append(big)

    small = ASSETS / "hellbot-48.png"
    image.resize((48, 48), Image.LANCZOS).convert("RGB").save(small, optimize=True)
    written.append(small)

    ico = ASSETS / "hellbot.ico"
    image.resize((256, 256), Image.LANCZOS).save(ico, format="ICO", sizes=ICO_SIZES)
    written.append(ico)

    return written


def main() -> int:
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else MASTER
    for path in build(source):
        size = path.stat().st_size
        print(f"wrote {path.relative_to(ROOT)}  ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
