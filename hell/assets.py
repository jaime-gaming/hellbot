"""Artwork resolution for embeds.

An image in `Announcements.py` may be either an `https://` URL or a file in the
project (e.g. `assets/hell-o-meter.png`).  Local files become `attachment://`
references, and the announcer uploads them alongside the embed automatically —
so the artwork can be swapped by dropping a new file in `assets/`, with no code
change and no image hosting.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Optional

import discord

from .paths import app_base

log = logging.getLogger("hell.assets")

ATTACHMENT = "attachment://"
_missing_warned: set[str] = set()


def resolve(spec: Optional[str]) -> Optional[str]:
    """Turn a config value into an embed-usable URL, or None if unusable."""
    text = (spec or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return text
    path = Path(text)
    if not path.is_absolute():
        path = app_base() / path
    if path.is_file():
        return ATTACHMENT + path.name
    if text not in _missing_warned:
        _missing_warned.add(text)
        log.warning("Image %r referenced in Announcements.py does not exist — skipping it", text)
    return None


def _find_file(name: str) -> Optional[Path]:
    for folder in (app_base() / "assets", app_base()):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _referenced_names(embeds: Iterable[discord.Embed]) -> list[str]:
    names: list[str] = []
    for embed in embeds:
        for url in (
            getattr(embed.thumbnail, "url", None),
            getattr(embed.image, "url", None),
            getattr(embed.author, "icon_url", None),
            getattr(embed.footer, "icon_url", None),
        ):
            if url and url.startswith(ATTACHMENT):
                name = url[len(ATTACHMENT) :]
                if name not in names:
                    names.append(name)
    return names


def files_for(embeds: Sequence[discord.Embed]) -> list[discord.File]:
    """Build the uploads an embed batch needs (deduplicated, never raises)."""
    files: list[discord.File] = []
    for name in _referenced_names(embeds):
        path = _find_file(name)
        if path is None:
            continue
        try:
            files.append(discord.File(str(path), filename=name))
        except OSError as exc:  # pragma: no cover - unreadable file
            log.warning("Could not attach %s: %s", path, exc)
    return files
