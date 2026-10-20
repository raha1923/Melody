from __future__ import annotations

import shutil
import sys
from pathlib import Path

_APP_NAME = "melodify"
_LEGACY_LINUX = Path.home() / ".local" / "share" / _APP_NAME


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def data_dir() -> Path:
    """Return the Melodify data directory for this OS.

    - macOS: ``~/Library/Application Support/melodify/``
    - Linux / other: ``~/.local/share/melodify/`` (XDG-style)

    On macOS, files still present under the Linux-style legacy path are
    copied into Application Support the first time they are requested.
    """
    if is_macos():
        path = Path.home() / "Library" / "Application Support" / _APP_NAME
    else:
        path = _LEGACY_LINUX
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_file(name: str) -> Path:
    """Path to a named file under ``data_dir()``, with legacy macOS migration."""
    target = data_dir() / name
    if target.exists():
        return target
    if is_macos():
        legacy = _LEGACY_LINUX / name
        if legacy.exists():
            try:
                shutil.copy2(legacy, target)
            except OSError:
                return legacy
    return target


def playback_path() -> Path:
    return data_file("playback.json")


def recent_path() -> Path:
    return data_file("recent.json")
