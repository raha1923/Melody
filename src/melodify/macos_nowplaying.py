from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from .mpris_bridge import MprisActions
from .models import Track as MelodifyTrack
from .player import StreamPlayer
from .queue import PlayQueue

log = logging.getLogger(__name__)


class MacNowPlaying:
    """macOS Now Playing + media keys via MediaPlayer (PyObjC).

    Optional — requires ``pip install pyobjc-framework-MediaPlayer``.
    Shows in Control Center / Lock Screen and handles play-pause/next/prev.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = False
        self._player: StreamPlayer | None = None
        self._queue: PlayQueue | None = None
        self._get_track: Callable[[], MelodifyTrack | None] = lambda: None
        self._actions: MprisActions | None = None
        self._info_center: Any = None
        self._cmd_center: Any = None
        self._last_status = ""
        self._last_track_id: int | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def attach(
        self,
        *,
        player: StreamPlayer,
        queue: PlayQueue,
        get_track: Callable[[], MelodifyTrack | None],
        actions: MprisActions,
    ) -> None:
        with self._lock:
            self._player = player
            self._queue = queue
            self._get_track = get_track
            self._actions = actions

    def start(self) -> bool:
        self.last_error = None
        if os.environ.get("MELODIFY_NOWPLAYING", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            self.last_error = "Disabled via MELODIFY_NOWPLAYING=0"
            return False
        if self._enabled:
            return True
        try:
            from MediaPlayer import (  # type: ignore[import-untyped]
                MPMusicPlaybackStatePaused,
                MPMusicPlaybackStatePlaying,
                MPMusicPlaybackStateStopped,
                MPNowPlayingInfoCenter,
                MPRemoteCommandCenter,
            )
        except ImportError as exc:
            self.last_error = (
                "PyObjC MediaPlayer missing — pip install pyobjc-framework-MediaPlayer "
                f"({exc})"
            )
            log.info("Now Playing unavailable: %s", exc)
            return False

        try:
            info = MPNowPlayingInfoCenter.defaultCenter()
            cmds = MPRemoteCommandCenter.sharedCommandCenter()
            cmds.playCommand().addTargetWithHandler_(self._h_play)
            cmds.pauseCommand().addTargetWithHandler_(self._h_pause)
            cmds.togglePlayPauseCommand().addTargetWithHandler_(self._h_toggle)
            cmds.nextTrackCommand().addTargetWithHandler_(self._h_next)
            cmds.previousTrackCommand().addTargetWithHandler_(self._h_prev)
            cmds.stopCommand().addTargetWithHandler_(self._h_stop)
            try:
                cmds.changePlaybackPositionCommand().addTargetWithHandler_(
                    self._h_seek
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Could not enable Now Playing: {exc}"
            log.info("Now Playing start failed: %s", exc)
            return False

        with self._lock:
            self._info_center = info
            self._cmd_center = cmds
            self._MPMusicPlaybackStatePlaying = MPMusicPlaybackStatePlaying
            self._MPMusicPlaybackStatePaused = MPMusicPlaybackStatePaused
            self._MPMusicPlaybackStateStopped = MPMusicPlaybackStateStopped
            self._enabled = True
        self.last_error = None
        log.info("macOS Now Playing enabled")
        self.notify(all_props=True)
        return True

    def stop(self) -> None:
        with self._lock:
            info = self._info_center
            self._info_center = None
            self._cmd_center = None
            self._enabled = False
        if info is not None:
            try:
                info.setPlaybackState_(self._MPMusicPlaybackStateStopped)
                info.setNowPlayingInfo_(None)
            except Exception:  # noqa: BLE001
                pass

    def notify(
        self,
        *,
        all_props: bool = False,
        title: bool = False,
        playpause: bool = False,
        seek: bool = False,
        volume: bool = False,
        ended: bool = False,
    ) -> None:
        if not self._enabled or self._info_center is None:
            return
        _ = (all_props, title, playpause, seek, volume, ended)
        try:
            self._push_state()
        except Exception:  # noqa: BLE001
            log.debug("Now Playing notify failed", exc_info=True)

    def sync_from_player(self) -> None:
        if not self._enabled:
            return
        player = self._player
        track = self._get_track()
        track_id = track.id if track else None
        if player is None or not player.is_alive:
            status = "stopped"
        elif player.is_paused:
            status = "paused"
        else:
            status = "playing"
        if track_id != self._last_track_id or status != self._last_status:
            self._last_track_id = track_id
            self._last_status = status
            self.notify(title=True, playpause=True)

    def _push_state(self) -> None:
        from Foundation import NSMutableDictionary  # type: ignore[import-untyped]
        from MediaPlayer import (  # type: ignore[import-untyped]
            MPMediaItemPropertyArtist,
            MPMediaItemPropertyPlaybackDuration,
            MPMediaItemPropertyTitle,
            MPNowPlayingInfoPropertyElapsedPlaybackTime,
            MPNowPlayingInfoPropertyPlaybackRate,
        )

        info = self._info_center
        player = self._player
        track = self._get_track()
        if info is None:
            return

        if player is None or not player.is_alive:
            info.setPlaybackState_(self._MPMusicPlaybackStateStopped)
            return

        if track is None:
            info.setPlaybackState_(
                self._MPMusicPlaybackStatePaused
                if player.is_paused
                else self._MPMusicPlaybackStatePlaying
            )
            return

        artists = ", ".join(a.name for a in track.artists) or "Unknown artist"
        payload = NSMutableDictionary.dictionary()
        payload[MPMediaItemPropertyTitle] = track.title
        payload[MPMediaItemPropertyArtist] = artists
        if track.duration and track.duration > 0:
            payload[MPMediaItemPropertyPlaybackDuration] = float(track.duration)
        pos = player.get_position()
        if pos is not None:
            payload[MPNowPlayingInfoPropertyElapsedPlaybackTime] = float(pos)
        payload[MPNowPlayingInfoPropertyPlaybackRate] = 0.0 if player.is_paused else 1.0
        info.setNowPlayingInfo_(payload)
        info.setPlaybackState_(
            self._MPMusicPlaybackStatePaused
            if player.is_paused
            else self._MPMusicPlaybackStatePlaying
        )

    def _run(self, fn: Callable[[MprisActions], None]) -> int:
        actions = self._actions
        if actions is None:
            return 0
        try:
            fn(actions)
        except Exception:  # noqa: BLE001
            log.debug("Now Playing action failed", exc_info=True)
        return 0

    def _h_play(self, _event: Any) -> int:
        return self._run(lambda a: a.resume())

    def _h_pause(self, _event: Any) -> int:
        return self._run(lambda a: a.pause())

    def _h_toggle(self, _event: Any) -> int:
        return self._run(lambda a: a.play_pause())

    def _h_next(self, _event: Any) -> int:
        return self._run(lambda a: a.next_track())

    def _h_prev(self, _event: Any) -> int:
        return self._run(lambda a: a.previous_track())

    def _h_stop(self, _event: Any) -> int:
        return self._run(lambda a: a.stop())

    def _h_seek(self, event: Any) -> int:
        try:
            seconds = float(event.positionTime())
        except Exception:  # noqa: BLE001
            return 0

        def _seek(actions: MprisActions) -> None:
            if actions.seek_to is not None:
                actions.seek_to(seconds)

        return self._run(_seek)
