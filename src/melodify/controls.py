from __future__ import annotations

import select
import sys
import termios
import tty
from collections.abc import Callable

from .player import PlayerError, StreamPlayer


def _format_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def interactive_playback_loop(
    player: StreamPlayer,
    *,
    on_position: Callable[[float], None] | None = None,
) -> None:
    """Simple nvlc-ish stdin control loop for CLI play/continue.

    Keys: space pause · ←/→ or [/] seek · -/= volume · s/q stop.
    No-op when stdin is not a TTY (falls back to wait-on-process).
    """
    if not sys.stdin.isatty() or not player.supports_ipc:
        try:
            if player._proc is not None:
                player._proc.wait()
        except KeyboardInterrupt:
            pos = player.stop()
            if on_position and pos is not None:
                on_position(pos)
            print("\nStopped.", flush=True)
        return

    print(
        "Controls: Space pause · ←/→ seek 5s · [/] seek 10s · -/= volume · s stop · q quit",
        flush=True,
    )
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while player.is_alive:
            ready, _, _ = select.select([sys.stdin], [], [], 0.4)
            if not ready:
                continue
            ch = sys.stdin.read(1)
            if not ch:
                continue
            try:
                if ch in (" ", "p"):
                    paused = player.toggle_pause()
                    status = player.status()
                    state = "Paused" if paused else "Playing"
                    print(
                        f"\r{state}  {_format_time(status.position)}"
                        f" / {_format_time(status.duration)}   ",
                        end="",
                        flush=True,
                    )
                elif ch in ("s", "q", "\x03"):
                    pos = player.stop()
                    if on_position and pos is not None:
                        on_position(pos)
                    print("\nStopped.", flush=True)
                    return
                elif ch == "\x1b":
                    # Arrow keys: ESC [ C/D
                    seq = sys.stdin.read(2)
                    if seq == "[C":
                        player.seek(5)
                    elif seq == "[D":
                        player.seek(-5)
                    _print_pos(player)
                elif ch == "]":
                    player.seek(10)
                    _print_pos(player)
                elif ch == "[":
                    player.seek(-10)
                    _print_pos(player)
                elif ch in ("=", "+"):
                    vol = player.volume_adjust(5)
                    print(f"\rVolume {vol:.0f}%   ", end="", flush=True)
                elif ch == "-":
                    vol = player.volume_adjust(-5)
                    print(f"\rVolume {vol:.0f}%   ", end="", flush=True)
            except PlayerError as exc:
                print(f"\n{exc}", flush=True)
                if not player.is_alive:
                    return
        print("\nTrack finished.", flush=True)
    except KeyboardInterrupt:
        pos = player.stop()
        if on_position and pos is not None:
            on_position(pos)
        print("\nStopped.", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if on_position and player.is_alive:
            pos = player.get_position()
            if pos is not None:
                on_position(pos)


def _print_pos(player: StreamPlayer) -> None:
    status = player.status()
    print(
        f"\r{_format_time(status.position)} / {_format_time(status.duration)}   ",
        end="",
        flush=True,
    )
