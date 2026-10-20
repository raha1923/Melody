from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import Track as MelodifyTrack
from .player import PlayerError, StreamPlayer
from .queue import PlayQueue

log = logging.getLogger(__name__)

# MPRIS positions / lengths are microseconds.
_US = 1_000_000


@dataclass
class MprisActions:
    """OS media-key callbacks. May be invoked from the D-Bus/GLib thread."""

    play_pause: Callable[[], None]
    pause: Callable[[], None]
    resume: Callable[[], None]
    stop: Callable[[], None]
    next_track: Callable[[], None]
    previous_track: Callable[[], None]
    # Absolute seek in seconds.
    seek_to: Callable[[float], None] | None = None
    # Volume 0.0–1.0.
    set_volume: Callable[[float], None] | None = None


class MelodifyMpris:
    """Optional MPRIS2 exporter so GNOME/KDE/playerctl can control Melodify."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: Any = None
        self._events: Any = None
        self._adapter: Any = None
        self._enabled = False
        self._player: StreamPlayer | None = None
        self._queue: PlayQueue | None = None
        self._get_track: Callable[[], MelodifyTrack | None] = lambda: None
        self._actions: MprisActions | None = None
        self._last_status = ""
        self._last_track_id: int | None = None

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
        """Publish on the session bus. Returns False if unavailable (headless, no D-Bus)."""
        if os.environ.get("MELODIFY_MPRIS", "1").strip() in {"0", "false", "no", "off"}:
            log.info("MPRIS disabled via MELODIFY_MPRIS")
            return False
        if self._enabled:
            return True
        try:
            from mpris_server.adapters import MprisAdapter
            from mpris_server.base import PlayState
            from mpris_server.events import EventAdapter
            from mpris_server.server import Server
        except ImportError as exc:
            log.info("MPRIS unavailable (install mpris-server): %s", exc)
            return False

        bridge = self

        class Adapter(MprisAdapter):
            def __init__(self) -> None:
                super().__init__("Melodify")

            def get_desktop_entry(self) -> str:
                return ""

            def can_quit(self) -> bool:
                return False

            def can_raise(self) -> bool:
                return False

            def can_fullscreen(self) -> bool:
                return False

            def has_tracklist(self) -> bool:
                return False

            def get_uri_schemes(self) -> list[str]:
                return ["http", "https"]

            def get_mime_types(self) -> list[str]:
                return ["audio/mpeg", "audio/mp4", "application/ogg"]

            def can_control(self) -> bool:
                return True

            def can_go_next(self) -> bool:
                q = bridge._queue
                return bool(q and q.peek_next() is not None)

            def can_go_previous(self) -> bool:
                q = bridge._queue
                return bool(q and q.peek_prev() is not None)

            def can_pause(self) -> bool:
                return bool(bridge._player and bridge._player.supports_ipc)

            def can_play(self) -> bool:
                return True

            def can_seek(self) -> bool:
                return bool(bridge._player and bridge._player.supports_ipc)

            def get_playstate(self) -> PlayState:
                player = bridge._player
                if player is None or not player.is_alive:
                    return PlayState.STOPPED
                if player.is_paused:
                    return PlayState.PAUSED
                return PlayState.PLAYING

            def get_current_position(self) -> int:
                player = bridge._player
                if player is None:
                    return 0
                pos = player.get_position()
                if pos is None:
                    return 0
                return max(0, int(pos * _US))

            def metadata(self) -> dict[str, Any]:
                track = bridge._get_track()
                if track is None:
                    return {
                        "mpris:trackid": "/org/mpris/MediaPlayer2/TrackList/NoTrack",
                        "xesam:title": "Melodify",
                    }
                artists = [a.name for a in track.artists] or ["Unknown artist"]
                meta: dict[str, Any] = {
                    "mpris:trackid": f"/org/mpris/MediaPlayer2/Track/{track.id}",
                    "xesam:title": track.title,
                    "xesam:artist": artists,
                    "xesam:albumArtist": artists[:1],
                }
                if track.duration and track.duration > 0:
                    meta["mpris:length"] = int(track.duration * _US)
                if track.image and str(track.image).startswith("http"):
                    meta["mpris:artUrl"] = str(track.image)
                return meta

            def get_volume(self) -> float:
                player = bridge._player
                if player is None:
                    return 1.0
                status = player.status()
                if status.volume is None:
                    return 1.0
                return max(0.0, min(1.0, float(status.volume) / 100.0))

            def is_mute(self) -> bool:
                return self.get_volume() <= 0.001

            def is_playlist(self) -> bool:
                return False

            def is_repeating(self) -> bool:
                return False

            def get_shuffle(self) -> bool:
                return False

            def get_stream_title(self) -> str:
                track = bridge._get_track()
                return track.title if track else "Melodify"

            def pause(self) -> None:
                bridge._run(lambda a: a.pause())

            def resume(self) -> None:
                bridge._run(lambda a: a.resume())

            def play(self) -> None:
                # PlayPause semantics when stopped → resume/replay current.
                bridge._run(lambda a: a.resume())

            def stop(self) -> None:
                bridge._run(lambda a: a.stop())

            def next(self) -> None:
                bridge._run(lambda a: a.next_track())

            def previous(self) -> None:
                bridge._run(lambda a: a.previous_track())

            def seek(self, time: int, track_id: str | None = None) -> None:
                seconds = max(0.0, float(time) / _US)

                def _seek(actions: MprisActions) -> None:
                    if actions.seek_to is not None:
                        actions.seek_to(seconds)
                    elif bridge._player is not None:
                        try:
                            bridge._player.seek(seconds, relative=False)
                        except PlayerError:
                            pass

                bridge._run(_seek)

            def set_volume(self, value: float) -> None:
                level = max(0.0, min(1.0, float(value)))

                def _vol(actions: MprisActions) -> None:
                    if actions.set_volume is not None:
                        actions.set_volume(level)
                    elif bridge._player is not None:
                        try:
                            bridge._player.set_volume(level * 100.0)
                        except PlayerError:
                            pass

                bridge._run(_vol)

            def set_repeating(self, value: bool) -> None:
                return None

            def set_loop_status(self, value: Any) -> None:
                return None

            def set_shuffle(self, value: bool) -> None:
                return None

            def set_rate(self, value: float) -> None:
                return None

            def open_uri(self, uri: str) -> None:
                return None

            def quit(self) -> None:
                return None

        class Events(EventAdapter):
            def emit_all(self) -> None:
                if self.player is not None:
                    self.on_player_all()

        adapter = Adapter()
        server = Server("Melodify", adapter)
        events = Events(
            root=server.root,
            player=server.player,
            playlists=server.playlists,
            tracklist=server.tracklist,
        )
        server.set_event_adapter(events)

        try:
            server.loop(background=True)
        except Exception as exc:  # noqa: BLE001 — no session bus / headless
            log.info("MPRIS not started (no D-Bus session?): %s", exc)
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
            return False

        with self._lock:
            self._adapter = adapter
            self._server = server
            self._events = events
            self._enabled = True
        log.info("MPRIS published as org.mpris.MediaPlayer2.Melodify")
        self.notify(all_props=True)
        return True

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
            self._events = None
            self._adapter = None
            self._enabled = False
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                log.debug("MPRIS quit failed", exc_info=True)

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
        if not self._enabled or self._events is None:
            return
        events = self._events
        try:
            if all_props:
                events.emit_all()
                return
            if ended:
                events.on_ended()
            if title:
                events.on_title()
            if playpause:
                events.on_playpause()
            if seek:
                pos = 0
                if self._player is not None:
                    raw = self._player.get_position()
                    if raw is not None:
                        pos = int(raw * _US)
                events.on_seek(pos)
            if volume:
                events.on_volume()
            # Always refresh playback + can_* when anything changes.
            if title or playpause or ended or seek:
                events.on_playback()
                events.on_options()
        except Exception:  # noqa: BLE001
            log.debug("MPRIS notify failed", exc_info=True)

    def sync_from_player(self) -> None:
        """Cheap poll helper — emit only when status/track actually change."""
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
        changed_title = track_id != self._last_track_id
        changed_status = status != self._last_status
        self._last_track_id = track_id
        self._last_status = status
        if changed_title or changed_status:
            self.notify(
                title=changed_title,
                playpause=changed_status,
                ended=(status == "stopped" and changed_status),
            )

    def _run(self, fn: Callable[[MprisActions], None]) -> None:
        actions = self._actions
        if actions is None:
            return
        try:
            fn(actions)
        except Exception:  # noqa: BLE001
            log.debug("MPRIS action failed", exc_info=True)


def build_direct_actions(
    player: StreamPlayer,
    queue: PlayQueue,
    *,
    play_track: Callable[[MelodifyTrack], None] | None = None,
    on_stop: Callable[[], None] | None = None,
) -> MprisActions:
    """Actions that talk to StreamPlayer/PlayQueue directly (CLI-friendly)."""

    def _next() -> None:
        if play_track is None:
            return
        nxt = queue.advance()
        if nxt is not None:
            play_track(nxt)

    def _prev() -> None:
        if play_track is None:
            return
        prev = queue.retreat()
        if prev is not None:
            play_track(prev)

    def _pause() -> None:
        try:
            player.pause()
        except PlayerError:
            pass

    def _resume() -> None:
        try:
            if player.is_alive and player.is_paused:
                player.resume()
            elif play_track is not None and queue.current is not None and not player.is_alive:
                play_track(queue.current)
            elif player.is_alive:
                player.resume()
        except PlayerError:
            pass

    def _play_pause() -> None:
        try:
            if not player.is_alive:
                _resume()
            else:
                player.toggle_pause()
        except PlayerError:
            pass

    def _stop() -> None:
        player.stop()
        if on_stop is not None:
            on_stop()

    def _seek_to(seconds: float) -> None:
        try:
            player.seek(seconds, relative=False)
        except PlayerError:
            pass

    def _set_volume(level: float) -> None:
        try:
            player.set_volume(level * 100.0)
        except PlayerError:
            pass

    return MprisActions(
        play_pause=_play_pause,
        pause=_pause,
        resume=_resume,
        stop=_stop,
        next_track=_next,
        previous_track=_prev,
        seek_to=_seek_to,
        set_volume=_set_volume,
    )
