from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class PlayerError(RuntimeError):
    pass


@dataclass
class PlayerBackend:
    name: str
    binary: str


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
    """Play remote audio via mpv/ffplay without buffering the whole file in Python."""

    def __init__(self, preferred: str = "mpv"):
        self.backend = detect_player(preferred)
        self._proc: subprocess.Popen[bytes] | None = None
        self.now_playing: str | None = None

    @property
    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        self._proc = None
        self.now_playing = None

    def play(self, url: str, *, title: str | None = None, http_headers: dict[str, str] | None = None) -> None:
        self.stop()
        headers = http_headers or {}
        header_blob = "\r\n".join(f"{k}: {v}" for k, v in headers.items())

        if self.backend.name == "mpv":
            cmd = [
                self.backend.binary,
                "--no-video",
                "--force-window=no",
                "--terminal=no",
                "--really-quiet",
                "--cache=yes",
                "--demuxer-max-bytes=8MiB",
                "--demuxer-readahead-secs=5",
            ]
            if title:
                cmd.append(f"--force-media-title={title}")
            if header_blob:
                cmd.append(f"--http-header-fields={header_blob}")
            cmd.append(url)
        else:
            # ffplay streams progressively; keep volume audible and hide UI.
            cmd = [
                self.backend.binary,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
            ]
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
            raise PlayerError(f"Failed to start {self.backend.name}: {exc}") from exc
        self.now_playing = title or url
