from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class PlayerError(RuntimeError):
    pass


@dataclass
class PlayerBackend:
    name: str
    binary: str


@dataclass
class PlaybackStatus:
    alive: bool
    paused: bool = False
    position: float | None = None
    duration: float | None = None
    volume: float | None = None
    title: str | None = None


def detect_player(preferred: str = "mpv") -> PlayerBackend:
    order = [preferred, "mpv", "ffplay"]
    seen: set[str] = set()
    for name in order:
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        binary = shutil.which(key)
        if binary:
            return PlayerBackend(name=key, binary=binary)
    raise PlayerError(
        "No stream player found. Install mpv (preferred) or ffmpeg/ffplay: "
        "sudo apt install mpv"
    )


class StreamPlayer:
    """Stream remote audio via mpv (JSON IPC) or ffplay.

    Python never buffers the media file — mpv/ffplay pull the HTTP stream.
    Pause/seek/volume require mpv IPC; ffplay only supports stop.
    """

    def __init__(self, preferred: str = "mpv"):
        self.backend = detect_player(preferred)
        self._proc: subprocess.Popen[bytes] | None = None
        self.now_playing: str | None = None
        self.start_seconds: float = 0.0
        self._ipc_path: Path | None = None
        self._paused = False
        self._volume = 100.0

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_playing(self) -> bool:
        return self.is_alive and not self._paused

    @property
    def is_paused(self) -> bool:
        return self.is_alive and self._paused

    @property
    def supports_ipc(self) -> bool:
        return self.backend.name == "mpv"

    def stop(self) -> float | None:
        """Stop playback. Returns last known position (seconds) when available."""
        position = self.get_position() if self.is_alive else None
        if self._proc is None:
            self._cleanup_ipc()
            self.now_playing = None
            self._paused = False
            return position
        if self._proc.poll() is None:
            # Prefer a clean quit over SIGTERM when IPC is up.
            if self.supports_ipc:
                try:
                    self._ipc_command(["quit"])
                    self._proc.wait(timeout=2)
                except (PlayerError, subprocess.TimeoutExpired):
                    pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
        self._proc = None
        self.now_playing = None
        self._paused = False
        self._cleanup_ipc()
        return position

    def play(
        self,
        url: str,
        *,
        title: str | None = None,
        http_headers: dict[str, str] | None = None,
        start_seconds: float = 0.0,
        volume: float | None = None,
    ) -> None:
        self.stop()
        headers = http_headers or {}
        header_blob = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        start = max(0.0, float(start_seconds or 0.0))
        if volume is not None:
            self._volume = max(0.0, min(130.0, float(volume)))

        if self.backend.name == "mpv":
            self._ipc_path = Path(tempfile.gettempdir()) / f"melodify-mpv-{os.getpid()}.sock"
            if self._ipc_path.exists():
                try:
                    self._ipc_path.unlink()
                except OSError:
                    pass
            cmd = [
                self.backend.binary,
                "--no-video",
                "--force-window=no",
                "--terminal=no",
                "--really-quiet",
                "--idle=no",
                "--cache=yes",
                "--demuxer-max-bytes=8MiB",
                "--demuxer-readahead-secs=5",
                f"--input-ipc-server={self._ipc_path}",
                f"--volume={self._volume}",
            ]
            if start > 0:
                cmd.append(f"--start={start}")
            if title:
                cmd.append(f"--force-media-title={title}")
            if header_blob:
                cmd.append(f"--http-header-fields={header_blob}")
            cmd.append(url)
        else:
            self._ipc_path = None
            cmd = [
                self.backend.binary,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
            ]
            if start > 0:
                cmd.extend(["-ss", str(start)])
            if header_blob:
                cmd.extend(["-headers", header_blob + "\r\n"])
            cmd.extend(["-i", url])

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._cleanup_ipc()
            raise PlayerError(f"Failed to start {self.backend.name}: {exc}") from exc

        self.now_playing = title or url
        self.start_seconds = start
        self._paused = False

        if self.supports_ipc:
            # Wait briefly for the socket to appear.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if self._ipc_path and self._ipc_path.exists():
                    break
                if not self.is_alive:
                    break
                time.sleep(0.05)

    def pause(self) -> None:
        self._require_ipc("pause")
        self._ipc_command(["set_property", "pause", True])
        self._paused = True

    def resume(self) -> None:
        self._require_ipc("resume")
        self._ipc_command(["set_property", "pause", False])
        self._paused = False

    def toggle_pause(self) -> bool:
        """Toggle pause. Returns True if now paused."""
        self._require_ipc("pause/resume")
        # Sync from mpv in case state drifted.
        current = self._ipc_get("pause")
        if isinstance(current, bool):
            self._paused = current
        self._ipc_command(["cycle", "pause"])
        self._paused = not self._paused
        # Re-read for accuracy.
        current = self._ipc_get("pause")
        if isinstance(current, bool):
            self._paused = current
        return self._paused

    def seek(self, seconds: float, *, relative: bool = True) -> float | None:
        self._require_ipc("seek")
        mode = "relative" if relative else "absolute"
        self._ipc_command(["seek", float(seconds), mode])
        return self.get_position()

    def volume_adjust(self, delta: float) -> float:
        self._require_ipc("volume")
        self._ipc_command(["add", "volume", float(delta)])
        volume = self._ipc_get("volume")
        if isinstance(volume, (int, float)):
            self._volume = float(volume)
        else:
            self._volume = max(0.0, min(130.0, self._volume + delta))
        return self._volume

    def set_volume(self, volume: float) -> float:
        self._require_ipc("volume")
        self._volume = max(0.0, min(130.0, float(volume)))
        self._ipc_command(["set_property", "volume", self._volume])
        return self._volume

    def get_position(self) -> float | None:
        if not self.is_alive or not self.supports_ipc:
            return None
        try:
            value = self._ipc_get("time-pos")
        except PlayerError:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def get_duration(self) -> float | None:
        if not self.is_alive or not self.supports_ipc:
            return None
        try:
            value = self._ipc_get("duration")
        except PlayerError:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def status(self) -> PlaybackStatus:
        if not self.is_alive:
            return PlaybackStatus(alive=False, title=self.now_playing)
        paused = self._paused
        position = None
        duration = None
        volume = self._volume
        if self.supports_ipc:
            try:
                raw_paused = self._ipc_get("pause")
                if isinstance(raw_paused, bool):
                    paused = raw_paused
                    self._paused = paused
                position = self.get_position()
                duration = self.get_duration()
                raw_volume = self._ipc_get("volume")
                if isinstance(raw_volume, (int, float)):
                    volume = float(raw_volume)
                    self._volume = volume
            except PlayerError:
                pass
        return PlaybackStatus(
            alive=True,
            paused=paused,
            position=position,
            duration=duration,
            volume=volume,
            title=self.now_playing,
        )

    def _require_ipc(self, feature: str) -> None:
        if not self.is_alive:
            raise PlayerError("Nothing is playing.")
        if not self.supports_ipc:
            raise PlayerError(
                f"{feature} needs mpv (JSON IPC). "
                "Install mpv or set MELODIFY_PLAYER=mpv — ffplay only supports stop."
            )

    def _cleanup_ipc(self) -> None:
        if self._ipc_path and self._ipc_path.exists():
            try:
                self._ipc_path.unlink()
            except OSError:
                pass
        self._ipc_path = None

    def _ipc_command(self, command: list[object], *, timeout: float = 1.5) -> dict:
        if not self._ipc_path:
            raise PlayerError("mpv IPC socket is not available.")
        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(self._ipc_path))
                sock.sendall(payload)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as exc:
            raise PlayerError(f"mpv IPC failed: {exc}") from exc

        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            raise PlayerError("Empty response from mpv IPC.")
        # mpv may emit multiple lines; take the first JSON object.
        line = raw.splitlines()[0]
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlayerError(f"Invalid mpv IPC response: {line[:120]}") from exc
        if data.get("error") not in (None, "success"):
            raise PlayerError(f"mpv IPC error: {data.get('error')}")
        return data

    def _ipc_get(self, prop: str) -> object:
        data = self._ipc_command(["get_property", prop])
        return data.get("data")
