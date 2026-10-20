from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from .paths import data_dir, is_linux, is_macos
from .player import detect_player


def create_media_bridge() -> Any:
    """Return the platform OS-media bridge (MPRIS on Linux, Now Playing on macOS)."""
    if is_macos():
        from .macos_nowplaying import MacNowPlaying

        return MacNowPlaying()
    from .mpris_bridge import MelodifyMpris

    return MelodifyMpris()


def diagnose_os_media() -> list[tuple[str, bool, str]]:
    """Platform-aware checks for ``melodify doctor``."""
    rows: list[tuple[str, bool, str]] = []
    rows.append(("Platform", True, sys.platform))
    rows.append(("Data dir", True, str(data_dir())))

    try:
        backend = detect_player("mpv")
        rows.append(("mpv", True, f"{backend.name} → {backend.binary}"))
    except Exception as exc:  # noqa: BLE001
        hint = (
            "brew install mpv"
            if is_macos()
            else "sudo apt install mpv"
        )
        rows.append(("mpv", False, f"{exc} — {hint}"))

    if is_macos():
        rows.extend(_diagnose_macos())
    elif is_linux():
        from .mpris_bridge import diagnose_os_media as diagnose_linux_mpris

        # Reuse Linux MPRIS diagnostics but drop duplicate mpv if present.
        for name, ok, detail in diagnose_linux_mpris():
            if name == "mpv":
                continue
            rows.append((name, ok, detail))
    else:
        rows.append(
            (
                "OS media",
                False,
                f"No media-key bridge for {sys.platform!r} (playback via mpv still works)",
            )
        )
    return rows


def _diagnose_macos() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    env = os.environ.get("MELODIFY_NOWPLAYING", "1").strip().lower()
    enabled = env not in {"0", "false", "no", "off"}
    rows.append(
        (
            "MELODIFY_NOWPLAYING",
            True,
            "enabled" if enabled else f"disabled ({env!r})",
        )
    )

    brew = shutil.which("brew")
    rows.append(
        (
            "Homebrew",
            True,
            brew or "optional — https://brew.sh (brew install mpv)",
        )
    )

    if not enabled:
        rows.append(("MediaPlayer (PyObjC)", True, "skipped (Now Playing disabled)"))
        rows.append(("Now Playing", True, "disabled via MELODIFY_NOWPLAYING=0"))
        return rows

    try:
        import MediaPlayer  # noqa: F401  # type: ignore[import-untyped]

        rows.append(("MediaPlayer (PyObjC)", True, "import ok"))
    except ImportError as exc:
        rows.append(
            (
                "MediaPlayer (PyObjC)",
                False,
                f"missing — pip install -e '.[macos]' ({exc})",
            )
        )
        rows.append(("Now Playing", False, "skipped (install PyObjC MediaPlayer first)"))
        return rows

    from .macos_nowplaying import MacNowPlaying

    probe = MacNowPlaying()
    try:
        ok = probe.start()
    except Exception as exc:  # noqa: BLE001
        rows.append(("Now Playing", False, f"crashed: {exc}"))
        try:
            probe.stop()
        except Exception:  # noqa: BLE001
            pass
        return rows
    if ok:
        rows.append(
            (
                "Now Playing",
                True,
                "Control Center / media keys — play something in Melodify to verify",
            )
        )
        probe.stop()
    else:
        rows.append(("Now Playing", False, probe.last_error or "failed"))

    return rows
