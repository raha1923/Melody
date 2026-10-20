from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

from .api import MelodifyAPIError, MelodifyClient
from .config import Settings
from .history import resolve_listening_history
from .models import Album, Artist, ListeningHistory, Track
from .mpris_bridge import MelodifyMpris, MprisActions
from .player import PlayerError, StreamPlayer
from .progress import ProgressStore
from .queue import PlayQueue
from .recent import RecentStore


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _call_ui(widget: Widget, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Marshal UI updates onto the App thread (Screens have no call_from_thread)."""
    app = widget.app
    try:
        app.call_from_thread(callback, *args, **kwargs)
    except RuntimeError:
        # Already on the app thread (or app not running) — apply directly.
        callback(*args, **kwargs)


class TrackSelected(Message):
    def __init__(
        self,
        track: Track,
        *,
        queue: list[Track] | None = None,
        source: str = "",
    ) -> None:
        self.track = track
        self.queue = list(queue or [track])
        self.source = source
        super().__init__()


class ArtistSelected(Message):
    def __init__(self, artist: Artist) -> None:
        self.artist = artist
        super().__init__()


class AlbumSelected(Message):
    def __init__(self, album: Album) -> None:
        self.album = album
        super().__init__()


class SelectableList(ListView):
    """ListView that posts domain selection messages."""

    BINDINGS = [
        *ListView.BINDINGS,
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, kind: str, *, source: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.kind = kind
        self.source = source
        self._items: list[Artist | Album | Track] = []

    def set_items(
        self,
        items: list[Artist | Album | Track],
        *,
        focus: bool = False,
    ) -> None:
        self._items = list(items)
        self.clear()
        if not items:
            self.append(ListItem(Label("Nothing here yet.")))
            self.index = None
            return
        for item in items:
            self.append(ListItem(Label(item.label())))
        self.index = 0
        self.scroll_home(animate=False)
        if focus:
            self.focus()

    def track_items(self) -> list[Track]:
        return [item for item in self._items if isinstance(item, Track)]

    def action_page_down(self) -> None:
        if not self._items:
            return
        step = max(1, self.size.height - 1)
        current = self.index if self.index is not None else 0
        self.index = min(len(self._items) - 1, current + step)

    def action_page_up(self) -> None:
        if not self._items:
            return
        step = max(1, self.size.height - 1)
        current = self.index if self.index is not None else 0
        self.index = max(0, current - step)

    def action_scroll_home(self) -> None:
        if self._items:
            self.index = 0

    def action_scroll_end(self) -> None:
        if self._items:
            self.index = len(self._items) - 1

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or index < 0 or index >= len(self._items):
            return
        item = self._items[index]
        if self.kind == "track" and isinstance(item, Track):
            self.post_message(
                TrackSelected(
                    item,
                    queue=self.track_items(),
                    source=self.source or self.id or "list",
                )
            )
        elif self.kind == "artist" and isinstance(item, Artist):
            self.post_message(ArtistSelected(item))
        elif self.kind == "album" and isinstance(item, Album):
            self.post_message(AlbumSelected(item))


class PlayerChrome(Vertical):
    """nvlc-style now-playing + progress + queue panel."""

    DEFAULT_CSS = """
    PlayerChrome {
        height: 1fr;
        width: 1fr;
        border-left: tall #334155;
        background: #0f172a;
        padding: 0 1;
    }
    #np-title {
        text-style: bold;
        color: #fde68a;
        height: 1;
    }
    #np-meta {
        color: #94a3b8;
        height: 1;
    }
    #np-time {
        color: #cbd5e1;
        height: 1;
    }
    #np-bar {
        height: 1;
        margin: 0 0 1 0;
    }
    #np-queue-label {
        color: #7dd3fc;
        height: 1;
    }
    #np-queue {
        height: 1fr;
        border: tall #1e293b;
        margin: 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Not playing — pick a song to start the queue.", id="np-title")
        yield Label("Space pause · n/b next/prev · ←/→ seek · -/= vol", id="np-meta")
        yield Label("--:-- / --:--", id="np-time")
        yield ProgressBar(id="np-bar", total=1000, show_eta=False, show_percentage=False)
        yield Label("Queue (empty)", id="np-queue-label")
        yield ListView(id="np-queue")

    def refresh_state(
        self,
        *,
        queue: PlayQueue,
        playing: bool,
        paused: bool,
        position: float | None,
        duration: float | None,
        volume: float | None,
    ) -> None:
        title = queue.current
        if title is None:
            self.query_one("#np-title", Label).update(
                "Not playing — pick a song to start the queue."
            )
            self.query_one("#np-meta", Label).update(
                "Space pause · n/b next/prev · ←/→ seek · -/= vol"
            )
            self.query_one("#np-time", Label).update("--:-- / --:--")
            bar = self.query_one("#np-bar", ProgressBar)
            bar.update(progress=0)
            self.query_one("#np-queue-label", Label).update("Queue (empty)")
            self._render_queue([])
            return

        state = "Paused" if paused else ("Playing" if playing else "Stopped")
        vol = f" · vol {volume:.0f}%" if volume is not None else ""
        self.query_one("#np-title", Label).update(f"{state}: {title.label()}")
        self.query_one("#np-meta", Label).update(
            f"Queue {queue.label()}{vol} · Space pause · n/b next/prev"
        )
        self.query_one("#np-time", Label).update(
            f"{_fmt_time(position)} / {_fmt_time(duration)}"
        )
        bar = self.query_one("#np-bar", ProgressBar)
        if duration and duration > 0 and position is not None:
            bar.update(progress=min(1000, int((position / duration) * 1000)))
        else:
            bar.update(progress=0)

        self.query_one("#np-queue-label", Label).update(
            f"Queue · {queue.label()} · auto-advances on end (no wrap)"
        )
        self._render_queue(queue.tracks, current_index=queue.index)

    def _render_queue(self, tracks: list[Track], *, current_index: int = -1) -> None:
        view = self.query_one("#np-queue", ListView)
        view.clear()
        if not tracks:
            view.append(ListItem(Label("Queue empty — play from search, artist, album, or last.")))
            return
        # Show a window around the current track so the list stays readable.
        start = max(0, current_index - 2)
        end = min(len(tracks), start + 8)
        start = max(0, end - 8)
        for i in range(start, end):
            track = tracks[i]
            marker = "▶ " if i == current_index else "  "
            view.append(ListItem(Label(f"{marker}{i + 1}. {track.label()}")))


class SearchScreen(Screen):
    BINDINGS = [
        Binding("slash", "focus_search", "Search", show=True),
        Binding("c", "continue_listening", "Continue", show=True),
        Binding("l", "load_last", "Last", show=True),
        Binding("escape", "clear_status", "Clear", show=False),
    ]

    status_text = reactive("Search Melodify, or press c / l for continue & last played.")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main-split"):
            with Vertical(id="browse-pane"):
                yield Label("Melodify", id="brand")
                yield Input(placeholder="Search Melodify…", id="search-input")
                with Horizontal(id="home-actions"):
                    yield Button("Continue", id="btn-continue", variant="primary")
                    yield Button("Last played", id="btn-last", variant="default")
                yield Static(self.status_text, id="status")
                yield LoadingIndicator(id="loading")
                with TabbedContent(id="results-tabs"):
                    with TabPane("Songs", id="tab-tracks"):
                        yield SelectableList("track", id="track-list", source="search")
                    with TabPane("Artists", id="tab-artists"):
                        yield SelectableList("artist", id="artist-list", source="artists")
                    with TabPane("Albums", id="tab-albums"):
                        yield SelectableList("album", id="album-list", source="albums")
                    with TabPane("Last", id="tab-last"):
                        yield SelectableList("track", id="last-list", source="last")
            yield PlayerChrome(id="player-chrome")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#loading", LoadingIndicator).display = False
        self.query_one("#search-input", Input).focus()
        self.run_worker(
            partial(self._load_last, silent=True),
            exclusive=False,
            thread=True,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-continue":
            self.action_continue_listening()
        elif event.button.id == "btn-last":
            self.action_load_last()

    def action_continue_listening(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        app.action_continue_listening()

    def action_load_last(self) -> None:
        self.run_worker(
            partial(self._load_last, silent=False),
            exclusive=True,
            thread=True,
        )

    def watch_status_text(self, value: str) -> None:
        try:
            self.query_one("#status", Static).update(value)
        except Exception:
            pass

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_status(self) -> None:
        self.status_text = "Ready."

    def set_loading(self, loading: bool, message: str | None = None) -> None:
        self.query_one("#loading", LoadingIndicator).display = loading
        if message is not None:
            self.status_text = message

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self.status_text = "Enter a search term first."
            return
        self.run_worker(partial(self._do_search, query), exclusive=True, thread=True)

    def _load_last(self, *, silent: bool) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        if not silent:
            _call_ui(self, self.set_loading, True, "Loading last played…")
        try:
            history = resolve_listening_history(app.client, app.recent)
        except MelodifyAPIError as exc:
            # Local list may still be usable even if online fallback fails.
            local = app.recent.list_tracks()
            if local:
                history = ListeningHistory(
                    tracks=local, source="local recently played", end=True
                )
            else:
                if not silent:
                    _call_ui(
                        self,
                        self.set_loading,
                        False,
                        f"Could not load last played: {exc}",
                    )
                return
        except Exception as exc:  # noqa: BLE001
            if not silent:
                _call_ui(self, self.set_loading, False, f"Unexpected error: {exc}")
            return

        def apply() -> None:
            last_list = self.query_one("#last-list", SelectableList)
            last_list.source = history.describe_source()
            last_list.set_items(history.tracks, focus=not silent)
            try:
                tabs = self.query_one("#results-tabs", TabbedContent)
                tabs.active = "tab-last"
            except Exception:
                pass
            if not history.tracks:
                self.set_loading(
                    False,
                    "No local recently-played tracks yet (and online history is empty). "
                    "Play something first — Melodify saves them under ~/.local/share/melodify/.",
                )
            else:
                self.set_loading(
                    False,
                    f"{len(history.tracks)} tracks from {history.describe_source()}. "
                    "↑↓/PgUp/PgDn scroll · Enter plays · c continues the first.",
                )

        _call_ui(self, apply)

    def _do_search(self, query: str) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        _call_ui(self, self.set_loading, True, f'Searching for “{query}”…')
        try:
            results = app.client.search(query)
        except MelodifyAPIError as exc:
            _call_ui(self, self.set_loading, False, f"Search failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            _call_ui(self, self.set_loading, False, f"Unexpected error: {exc}")
            return

        def apply() -> None:
            tracks = self.query_one("#track-list", SelectableList)
            tracks.set_items(results.tracks, focus=bool(results.tracks))
            self.query_one("#artist-list", SelectableList).set_items(results.artists)
            self.query_one("#album-list", SelectableList).set_items(results.albums)
            total = len(results.tracks) + len(results.artists) + len(results.albums)
            if total == 0:
                self.set_loading(False, f'No results for “{query}”. Try another spelling.')
            else:
                self.set_loading(
                    False,
                    f"Found {len(results.tracks)} songs, {len(results.artists)} artists, "
                    f"{len(results.albums)} albums. ↑↓/PgUp/PgDn scroll · Enter opens · p plays.",
                )
            try:
                tabs = self.query_one("#results-tabs", TabbedContent)
                tabs.active = "tab-tracks"
            except Exception:
                pass

        _call_ui(self, apply)


class ArtistScreen(Screen):
    def __init__(self, artist: Artist) -> None:
        super().__init__()
        self.artist = artist

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main-split"):
            with Vertical(id="browse-pane"):
                yield Label(self.artist.name, id="detail-title")
                yield Label(self.artist.label(), id="detail-meta")
                yield Static("Loading artist…", id="status")
                yield LoadingIndicator(id="loading")
                with Horizontal(id="detail-actions"):
                    yield Button("Back", id="btn-back", variant="default")
                    yield Button("Refresh", id="btn-refresh", variant="primary")
                with TabbedContent():
                    with TabPane("Songs", id="tab-artist-tracks"):
                        yield SelectableList(
                            "track", id="artist-track-list", source="artist"
                        )
                    with TabPane("Albums", id="tab-artist-albums"):
                        yield SelectableList(
                            "album", id="artist-album-list", source="artist-albums"
                        )
            yield PlayerChrome(id="player-chrome")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load, exclusive=True, thread=True)
        app = self.app
        if isinstance(app, MelodifyApp):
            app.refresh_player_chrome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-refresh":
            self.run_worker(self._load, exclusive=True, thread=True)

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        _call_ui(self, self._set_status, "Fetching artist catalog…", loading=True)
        try:
            artist = app.client.get_artist(self.artist.id)
            tracks = app.client.get_artist_tracks_all(
                self.artist.id, sort_by="popular"
            )
            albums = app.client.get_artist_albums_all(self.artist.id)
        except MelodifyAPIError as exc:
            _call_ui(self, self._set_status, f"Could not load artist: {exc}", loading=False)
            return

        def apply() -> None:
            self.artist = artist
            self.query_one("#detail-title", Label).update(artist.name)
            self.query_one("#detail-meta", Label).update(artist.label())
            track_list = self.query_one("#artist-track-list", SelectableList)
            track_list.set_items(tracks, focus=bool(tracks))
            self.query_one("#artist-album-list", SelectableList).set_items(albums)
            if not tracks and not albums:
                self._set_status("This artist has no songs or albums available.", loading=False)
            else:
                self._set_status(
                    f"{len(tracks)} songs · {len(albums)} albums. "
                    "↑↓/PgUp/PgDn scroll · Enter plays · fills queue.",
                    loading=False,
                )

        _call_ui(self, apply)


class AlbumScreen(Screen):
    def __init__(self, album: Album) -> None:
        super().__init__()
        self.album = album

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main-split"):
            with Vertical(id="browse-pane"):
                yield Label(self.album.title, id="detail-title")
                yield Label(self.album.label(), id="detail-meta")
                yield Static("Loading album…", id="status")
                yield LoadingIndicator(id="loading")
                with Horizontal(id="detail-actions"):
                    yield Button("Back", id="btn-back", variant="default")
                    yield Button("Refresh", id="btn-refresh", variant="primary")
                yield SelectableList("track", id="album-track-list", source="album")
            yield PlayerChrome(id="player-chrome")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load, exclusive=True, thread=True)
        app = self.app
        if isinstance(app, MelodifyApp):
            app.refresh_player_chrome()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-refresh":
            self.run_worker(self._load, exclusive=True, thread=True)

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        _call_ui(self, self._set_status, "Fetching album tracks…", loading=True)
        try:
            tracks = app.client.get_album_tracks_all(self.album.id)
        except MelodifyAPIError as exc:
            _call_ui(self, self._set_status, f"Could not load album: {exc}", loading=False)
            return

        def apply() -> None:
            self.query_one("#album-track-list", SelectableList).set_items(
                tracks, focus=bool(tracks)
            )
            if not tracks:
                self._set_status("This album has no playable tracks.", loading=False)
            else:
                self._set_status(
                    f"{len(tracks)} tracks. ↑↓/PgUp/PgDn scroll · Enter or p plays.",
                    loading=False,
                )

        _call_ui(self, apply)


class MelodifyApp(App[None]):
    TITLE = "Melodify"
    SUB_TITLE = "terminal player"
    CSS = """
    Screen {
        background: #0b1020;
        color: #e8eefc;
    }
    #main-split {
        height: 1fr;
    }
    #browse-pane {
        width: 3fr;
        height: 1fr;
    }
    #brand {
        text-style: bold;
        color: #7dd3fc;
        margin: 1 1 0 1;
    }
    #search-input {
        margin: 0 1 1 1;
    }
    #home-actions, #detail-actions {
        height: auto;
        margin: 0 1 1 1;
    }
    #status {
        margin: 0 1 1 1;
        color: #cbd5e1;
        height: 1;
    }
    #loading {
        margin: 0 1 1 1;
        height: 1;
    }
    #detail-title {
        text-style: bold;
        color: #fde68a;
        margin: 1 1 0 1;
    }
    #detail-meta {
        color: #94a3b8;
        margin: 0 1 1 1;
    }
    #results-tabs, #browse-pane TabbedContent {
        height: 1fr;
    }
    TabPane {
        height: 1fr;
        layout: vertical;
    }
    #browse-pane ListView {
        height: 1fr;
        border: tall #1e293b;
        margin: 0 1 1 1;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    #player-chrome {
        width: 2fr;
        height: 1fr;
        min-width: 28;
    }
    ListItem {
        padding: 0 1;
        height: 1;
    }
    ListItem.--highlight {
        background: #1d4ed8;
    }
    Footer {
        background: #111827;
    }
    Header {
        background: #111827;
        color: #e2e8f0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "play_highlighted", "Play"),
        Binding("c", "continue_listening", "Continue"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "play_next", "Next"),
        Binding("b", "play_prev", "Prev"),
        Binding(">", "play_next", "Next", show=False),
        Binding("<", "play_prev", "Prev", show=False),
        Binding("s", "stop_playback", "Stop"),
        Binding("left", "seek_back", "Seek -5s"),
        Binding("right", "seek_forward", "Seek +5s"),
        Binding("shift+left", "seek_back_10", "Seek -10s"),
        Binding("shift+right", "seek_forward_10", "Seek +10s"),
        Binding("minus", "volume_down", "Vol -"),
        Binding("equals", "volume_up", "Vol +"),
        Binding("plus", "volume_up", "Vol +", show=False),
        Binding("backspace", "go_back", "Back"),
    ]

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.client = MelodifyClient(settings)
        self.player = StreamPlayer(preferred=settings.player)
        self.progress = ProgressStore()
        self.recent = RecentStore()
        self.queue = PlayQueue()
        self.mpris = MelodifyMpris()
        self._last_track: Track | None = None
        self._expect_playback = False
        self._saw_alive = False
        self._playback_started_at = 0.0
        self._auto_advance = True

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())
        self.set_interval(0.5, self._on_player_tick)
        self._start_mpris()
        missing = self.settings.missing()
        if missing:
            self.notify(
                "Missing env: " + ", ".join(missing),
                severity="error",
                timeout=8,
            )

    def on_unmount(self) -> None:
        self._expect_playback = False
        self._persist_current_position(mark_recent=True)
        self.mpris.stop()
        self.player.stop()
        self.client.close()

    def _start_mpris(self) -> None:
        app = self

        def _call(action: str) -> None:
            # D-Bus thread → Textual thread.
            try:
                app.call_from_thread(getattr(app, action))
            except RuntimeError:
                getattr(app, action)()

        def _call_seek(seconds: float) -> None:
            def run() -> None:
                try:
                    self.player.seek(seconds, relative=False)
                    if self._last_track is not None:
                        pos = self.player.get_position()
                        if pos is not None:
                            self.progress.save_position(
                                self._last_track.id,
                                pos,
                                title=self._last_track.title,
                                mark_last=True,
                            )
                    self.refresh_player_chrome()
                    self.mpris.notify(seek=True)
                except PlayerError as exc:
                    self.notify(str(exc), severity="warning", timeout=3)

            try:
                self.call_from_thread(run)
            except RuntimeError:
                run()

        def _call_vol(level: float) -> None:
            def run() -> None:
                try:
                    self.player.set_volume(level * 100.0)
                    self.refresh_player_chrome()
                    self.mpris.notify(volume=True)
                except PlayerError as exc:
                    self.notify(str(exc), severity="warning", timeout=3)

            try:
                self.call_from_thread(run)
            except RuntimeError:
                run()

        self.mpris.attach(
            player=self.player,
            queue=self.queue,
            get_track=lambda: self._last_track,
            actions=MprisActions(
                play_pause=lambda: _call("action_toggle_pause"),
                pause=lambda: _call("_mpris_pause"),
                resume=lambda: _call("_mpris_resume"),
                stop=lambda: _call("action_stop_playback"),
                next_track=lambda: _call("action_play_next"),
                previous_track=lambda: _call("action_play_prev"),
                seek_to=_call_seek,
                set_volume=_call_vol,
            ),
        )

        if self.mpris.start():
            self.notify("OS media controls (MPRIS) enabled.", timeout=3)

    def _mpris_pause(self) -> None:
        try:
            self.player.pause()
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        status = self.player.status()
        if self._last_track is not None and status.position is not None:
            self.progress.save_position(
                self._last_track.id,
                status.position,
                title=self._last_track.title,
                mark_last=True,
            )
            self.recent.upsert(self._last_track, position_seconds=status.position)
        self.refresh_player_chrome()
        self.mpris.notify(playpause=True)

    def _mpris_resume(self) -> None:
        try:
            if self.player.is_alive and self.player.is_paused:
                self.player.resume()
            elif self._last_track is not None and not self.player.is_alive:
                self._play_track(self._last_track, from_queue=True)
                return
            elif self.player.is_alive:
                self.player.resume()
            else:
                self.notify("Nothing to resume — pick a song first.", severity="warning")
                return
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        self.refresh_player_chrome()
        self.mpris.notify(playpause=True)

    def _persist_current_position(self, *, mark_recent: bool = True) -> float | None:
        """Save scrubber position (and optionally upsert recent) for the current track."""
        if self._last_track is None:
            return None
        status = self.player.status()
        position = status.position
        if position is None:
            position = self.progress.get_position(self._last_track.id)
        if position is None:
            if mark_recent:
                self.recent.upsert(self._last_track)
            return None
        self.progress.save_position(
            self._last_track.id,
            position,
            title=self._last_track.title,
            mark_last=True,
        )
        if mark_recent:
            self.recent.upsert(self._last_track, position_seconds=position)
        return position

    def refresh_player_chrome(self) -> None:
        try:
            chrome = self.screen.query_one("#player-chrome", PlayerChrome)
        except Exception:
            return
        status = self.player.status()
        chrome.refresh_state(
            queue=self.queue,
            playing=status.alive and not status.paused,
            paused=status.paused,
            position=status.position,
            duration=status.duration
            or (float(self._last_track.duration) if self._last_track and self._last_track.duration else None),
            volume=status.volume,
        )

    def _on_player_tick(self) -> None:
        self.refresh_player_chrome()
        self._watch_playback_end()
        self.mpris.sync_from_player()

    def action_go_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def action_stop_playback(self) -> None:
        self._expect_playback = False
        position = self.player.stop()
        if self._last_track is not None:
            if position is None:
                position = self.progress.get_position(self._last_track.id) or 0.0
            self.progress.save_position(
                self._last_track.id,
                position,
                title=self._last_track.title,
                mark_last=True,
            )
            self.recent.upsert(self._last_track, position_seconds=position)
        self.refresh_player_chrome()
        self.mpris.notify(playpause=True, ended=True)
        self.notify("Playback stopped.", timeout=2)

    def action_play_next(self) -> None:
        self._expect_playback = False
        track = self.queue.advance()
        if track is None:
            self.notify("End of queue — no next track.", severity="information", timeout=2)
            return
        self._play_track(track, start_seconds=0, from_queue=True)

    def action_play_prev(self) -> None:
        self._expect_playback = False
        track = self.queue.retreat()
        if track is None:
            self.notify(
                "Start of queue — no previous track.",
                severity="information",
                timeout=2,
            )
            return
        self._play_track(track, start_seconds=0, from_queue=True)

    def _watch_playback_end(self) -> None:
        if not self._expect_playback or not self._auto_advance:
            return
        if self.player.is_alive:
            self._saw_alive = True
            return
        # Ignore the brief window before mpv is up, otherwise we'd skip tracks.
        if not self._saw_alive or (time.time() - self._playback_started_at) < 1.5:
            return
        self._expect_playback = False
        self._saw_alive = False
        if self._last_track is not None:
            # Track finished — keep it at the front of local recent, clear scrubber.
            self.recent.upsert(self._last_track, position_seconds=0.0)
            self.progress.clear_position(self._last_track.id)
        if self.queue.peek_next() is None:
            self.refresh_player_chrome()
            self.notify("Queue finished.", timeout=2)
            return
        track = self.queue.advance()
        if track is not None:
            self.notify(f"Next: {track.title}", timeout=2)
            self._play_track(track, start_seconds=0, from_queue=True)

    def action_toggle_pause(self) -> None:
        try:
            paused = self.player.toggle_pause()
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        status = self.player.status()
        if paused and self._last_track is not None and status.position is not None:
            self.progress.save_position(
                self._last_track.id,
                status.position,
                title=self._last_track.title,
                mark_last=True,
            )
            self.recent.upsert(self._last_track, position_seconds=status.position)
        self.refresh_player_chrome()
        self.mpris.notify(playpause=True)
        label = "Paused" if paused else "Playing"
        pos = ""
        if status.position is not None:
            pos = f" @ {_fmt_time(status.position)}"
        self.notify(f"{label}{pos}", timeout=2)

    def action_seek_back(self) -> None:
        self._seek(-5)

    def action_seek_forward(self) -> None:
        self._seek(5)

    def action_seek_back_10(self) -> None:
        self._seek(-10)

    def action_seek_forward_10(self) -> None:
        self._seek(10)

    def action_volume_up(self) -> None:
        self._volume(5)

    def action_volume_down(self) -> None:
        self._volume(-5)

    def _seek(self, delta: float) -> None:
        try:
            position = self.player.seek(delta)
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        self.refresh_player_chrome()
        if position is None:
            self.notify(f"Seek {delta:+.0f}s", timeout=1)
            return
        self.notify(f"Seek → {_fmt_time(position)}", timeout=1)
        if self._last_track is not None:
            self.progress.save_position(
                self._last_track.id,
                position,
                title=self._last_track.title,
                mark_last=True,
            )
            self.recent.upsert(self._last_track, position_seconds=position)

    def _volume(self, delta: float) -> None:
        try:
            volume = self.player.volume_adjust(delta)
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        self.refresh_player_chrome()
        self.notify(f"Volume {volume:.0f}%", timeout=1)

    def action_continue_listening(self) -> None:
        self.run_worker(self._continue_worker, exclusive=True, thread=True)

    def action_play_highlighted(self) -> None:
        context = self._highlighted_track_context()
        if context is None:
            if self._last_track is not None:
                self._play_track(self._last_track, from_queue=True)
            else:
                self.notify("Highlight a song first.", severity="warning")
            return
        track, queue_tracks, source = context
        self.queue.load(queue_tracks, start=track, source=source)
        self._play_track(track, from_queue=True)

    def on_track_selected(self, event: TrackSelected) -> None:
        self.queue.load(event.queue, start=event.track, source=event.source or "list")
        self._play_track(event.track, from_queue=True)

    def on_artist_selected(self, event: ArtistSelected) -> None:
        self.push_screen(ArtistScreen(event.artist))

    def on_album_selected(self, event: AlbumSelected) -> None:
        self.push_screen(AlbumScreen(event.album))

    def _highlighted_track_context(
        self,
    ) -> tuple[Track, list[Track], str] | None:
        screen = self.screen
        for list_id in (
            "#track-list",
            "#artist-track-list",
            "#album-track-list",
            "#last-list",
        ):
            try:
                widget = screen.query_one(list_id, SelectableList)
            except Exception:
                continue
            if widget.kind != "track":
                continue
            index = widget.index
            if index is None or index < 0 or index >= len(widget._items):
                continue
            item = widget._items[index]
            if isinstance(item, Track):
                return item, widget.track_items(), widget.source or list_id
        return None

    def _play_track(
        self,
        track: Track,
        *,
        start_seconds: float | None = None,
        from_queue: bool = False,
    ) -> None:
        if not from_queue and self.queue.current is None:
            self.queue.load([track], start=track, source="single")
        self._last_track = track
        self.run_worker(
            partial(self._play_worker, track, start_seconds=start_seconds),
            exclusive=True,
            thread=True,
        )

    def _continue_worker(self) -> None:
        self.call_from_thread(self.notify, "Loading continue listening…", timeout=2)
        try:
            history = resolve_listening_history(self.client, self.recent)
        except MelodifyAPIError as exc:
            local = self.recent.list_tracks()
            if local:
                history = ListeningHistory(
                    tracks=local, source="local recently played", end=True
                )
            else:
                self.call_from_thread(
                    self.notify,
                    f"Continue failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
        if not history.tracks:
            self.call_from_thread(
                self.notify,
                "Nothing to continue — play something first (saved under ~/.local/share/melodify/).",
                severity="warning",
                timeout=5,
            )
            return
        track = self.queue.load(
            history.tracks,
            start=history.tracks[0],
            source=history.describe_source(),
        )
        if track is None:
            return
        self._last_track = track
        start = self.progress.resolve_start_seconds(
            track.id,
            api_progress=track.progress_seconds,
            duration=track.duration,
        )
        self._play_worker(
            track,
            start_seconds=start,
            history_source=history.describe_source(),
        )

    def _play_worker(
        self,
        track: Track,
        *,
        start_seconds: float | None = None,
        history_source: str | None = None,
    ) -> None:
        title = track.label()
        if start_seconds is None:
            start_seconds = self.progress.resolve_start_seconds(
                track.id,
                api_progress=track.progress_seconds,
                duration=track.duration,
            )
        resume_note = ""
        if start_seconds > 0:
            resume_note = f" @ {_fmt_time(start_seconds)}"
        prefix = "Continuing" if history_source else "Starting"
        detail = f" ({history_source})" if history_source else ""
        self.call_from_thread(
            self.notify,
            f"{prefix}{detail}: {track.title}{resume_note} · {self.queue.label()}",
            timeout=3,
        )
        try:
            self._expect_playback = False
            url = self.client.build_play_url(track)
            headers = {
                "authorization": self.settings.authorization,
                "user-id": self.settings.user_id,
                "device-id": self.settings.device_id,
                "device-token": self.settings.device_token,
                "Origin": self.settings.origin,
                "Referer": f"{self.settings.origin}/",
            }
            self.client.record_listen(track)
            seek = float(start_seconds or 0)
            self.progress.save_position(
                track.id,
                seek,
                title=track.title,
                mark_last=True,
            )
            self.recent.upsert(track, position_seconds=seek)
            self.player.play(
                url,
                title=title,
                http_headers=headers,
                start_seconds=seek,
            )
            self._saw_alive = False
            self._playback_started_at = time.time()
            self._expect_playback = True
        except (MelodifyAPIError, PlayerError) as exc:
            self._expect_playback = False
            self._saw_alive = False
            self.call_from_thread(
                self.notify,
                f"Play failed: {exc}",
                severity="error",
                timeout=6,
            )
            return
        self.call_from_thread(self.refresh_player_chrome)
        self.call_from_thread(self.mpris.notify, title=True, playpause=True)
        self.call_from_thread(
            self.notify,
            f"Playing: {track.title} [{self.queue.label()}]",
            timeout=3,
        )


def run_tui(settings: Settings) -> None:
    MelodifyApp(settings).run()
