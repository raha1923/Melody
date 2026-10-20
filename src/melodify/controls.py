from __future__ import annotations

import select
import sys
import termios
import tty
from collections.abc import Callable

from .models import Track
from .player import PlayerError, StreamPlayer
from .queue import PlayQueue


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
    queue: PlayQueue | None = None,
    play_track: Callable[[Track], None] | None = None,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """TTY control loop for CLI play/continue.

    Keys: space pause · n/> next · b/< prev · ←/→ [/] seek · -/= volume · s/q stop.
    Queue edges do not wrap. When the current track ends, auto-advances if possible.
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
        if (
            queue is not None
            and play_track is not None
            and not player.is_alive
            and queue.peek_next() is not None
        ):
            nxt = queue.advance()
            if nxt is not None:
                print(f"Next: {nxt.label()}", flush=True)
                play_track(nxt)
                interactive_playback_loop(
                    player,
                    on_position=on_position,
                    queue=queue,
                    play_track=play_track,
                    on_tick=on_tick,
                )
        return

    print(
        "Controls: Space pause · n/> next · b/< prev · ←/→ seek · -/= vol · s stop",
        flush=True,
    )
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:  # noqa: BLE001
                    pass
            if not player.is_alive:
                if (
                    queue is not None
                    and play_track is not None
                    and queue.peek_next() is not None
                ):
                    nxt = queue.advance()
                    if nxt is not None:
                        print(f"\nNext: {nxt.label()}", flush=True)
                        play_track(nxt)
                        continue
                print("\nQueue finished." if queue and queue.size else "\nTrack finished.", flush=True)
                return

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
                    if on_tick is not None:
                        on_tick()
                elif ch in ("s", "q", "\x03"):
                    pos = player.stop()
                    if on_position and pos is not None:
                        on_position(pos)
                    print("\nStopped.", flush=True)
                    return
                elif ch in ("n", ">"):
                    if queue is None or play_track is None:
                        print("\nNo queue loaded.", flush=True)
                        continue
                    nxt = queue.advance()
                    if nxt is None:
                        print("\nEnd of queue — no next track.", flush=True)
                        continue
                    pos = player.get_position()
                    if on_position and pos is not None:
                        on_position(pos)
                    print(f"\nNext [{queue.label()}]: {nxt.label()}", flush=True)
                    play_track(nxt)
                elif ch in ("b", "<"):
                    if queue is None or play_track is None:
                        print("\nNo queue loaded.", flush=True)
                        continue
                    prev = queue.retreat()
                    if prev is None:
                        print("\nStart of queue — no previous track.", flush=True)
                        continue
                    pos = player.get_position()
                    if on_position and pos is not None:
                        on_position(pos)
                    print(f"\nPrev [{queue.label()}]: {prev.label()}", flush=True)
                    play_track(prev)
                elif ch == "\x1b":
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
                if not player.is_alive and (queue is None or queue.peek_next() is None):
                    return
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
