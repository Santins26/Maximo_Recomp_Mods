"""Resolve bundled image assets (app icon, logo, background).

Works both when running from source (paths relative to the repo root) and
when frozen into a single-file PyInstaller exe (paths relative to the
temporary extraction dir, ``sys._MEIPASS``). PyInstaller's ``--add-data``
copies each folder next to the exe's internals at runtime; we mirror the
exact same relative layout in both cases so one lookup function works
everywhere.
"""
from __future__ import annotations
import sys
from pathlib import Path


def _base_dir() -> Path:
    """Root to resolve bundled asset folders from."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # Running from source: this file lives in <root>/randomizer/assets.py
    return Path(__file__).resolve().parent.parent


def _first_existing(folder: str) -> Path | None:
    base = _base_dir() / folder
    if not base.is_dir():
        return None
    for p in sorted(base.iterdir()):
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".ico", ".bmp"):
            return p
    return None


def logo_path() -> Path | None:
    """Image shown in the GUI header (from the ``logo`` folder)."""
    return _first_existing("logo")


def background_path() -> Path | None:
    """Image used as the GUI background (from the ``background`` folder)."""
    return _first_existing("background")


def app_icon_path() -> Path | None:
    """Windows .ico for the window titlebar / taskbar."""
    p = _base_dir() / "app.ico"
    return p if p.is_file() else None
