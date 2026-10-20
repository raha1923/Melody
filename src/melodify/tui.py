from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)

from .api import MelodifyAPIError, MelodifyClient
from .config import Settings
from .models import Album, Artist, Track
from .player import PlayerError, StreamPlayer
from .progress import ProgressStore
from .queue import PlayQueue


def _status_line(text: str, *, tone: str = "dim") -> Static:
    return Static(text, classes=f"status status-{tone}", id="status")


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

    def __init__(self, kind: str, *, source: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.kind = kind
        self.source = source
        self._items: list[Artist | Album | Track] = []

    def set_items(self, items: list[Artist | Album | Track]) -> None:
        self._items = list(items)
        self.clear()
        if not items:
            self.append(ListItem(Label("Nothing here yet.")))
            return
        for item in items:
            self.append(ListItem(Label(item.label())))

    def track_items(self) -> list[Track]:
        return [item for item in self._items if isinstance(item, Track)]

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


class SearchScreen(Screen):
    BINDINGS = [
        Binding("slash", "focus_search", "Search", show=True),
        Binding("c", "continue_listening", "Continue", show=True),
        Binding("l", "load_last", "Last", show=True),
        Binding("escape", "clear_status", "Clear", show=False),
    ]

    status_text = reactive(
        "Search Melodify, or press c to continue listening / l for last played."
    )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="search-layout"):
            yield Label("Melodify", id="brand")
            yield Label("Search artists, albums, and songs from your terminal.", id="tagline")
            yield Input(
                placeholder="Search Melodify…",
                id="search-input",
            )
            with Horizontal(id="home-actions"):
                yield Button("Continue listening", id="btn-continue", variant="primary")
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
                with TabPane("Last played", id="tab-last"):
                    yield SelectableList("track", id="last-list", source="last")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#loading", LoadingIndicator).display = False
        self.query_one("#search-input", Input).focus()
        self.run_worker(self._load_last(silent=True), exclusive=False, thread=True)

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
        self.run_worker(self._load_last(silent=False), exclusive=True, thread=True)

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
        self.run_worker(self._do_search(query), exclusive=True, thread=True)

    def _load_last(self, *, silent: bool) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        if not silent:
            self.call_from_thread(
                self.set_loading, True, "Loading online listening history…"
            )
        try:
            history = app.client.get_listening_history()
        except MelodifyAPIError as exc:
            if not silent:
                self.call_from_thread(
                    self.set_loading, False, f"Could not load last played: {exc}"
                )
            return
        except Exception as exc:  # noqa: BLE001
            if not silent:
                self.call_from_thread(
                    self.set_loading, False, f"Unexpected error: {exc}"
                )
            return

        def apply() -> None:
            self.query_one("#last-list", SelectableList).set_items(history.tracks)
            try:
                tabs = self.query_one("#results-tabs", TabbedContent)
                tabs.active = "tab-last"
            except Exception:
                pass
            if not history.tracks:
                self.set_loading(
                    False,
                    "No online listening history yet. Play tracks so Melodify can record listens.",
                )
            else:
                self.set_loading(
                    False,
                    f"{len(history.tracks)} tracks from {history.describe_source()}. "
                    "Enter plays · c continues the first.",
                )

        self.call_from_thread(apply)

    def _do_search(self, query: str) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        self.call_from_thread(self.set_loading, True, f'Searching for “{query}”…')
        try:
            results = app.client.search(query)
        except MelodifyAPIError as exc:
            self.call_from_thread(self.set_loading, False, f"Search failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors in TUI
            self.call_from_thread(self.set_loading, False, f"Unexpected error: {exc}")
            return

        def apply() -> None:
            self.query_one("#track-list", SelectableList).set_items(results.tracks)
            self.query_one("#artist-list", SelectableList).set_items(results.artists)
            self.query_one("#album-list", SelectableList).set_items(results.albums)
            total = len(results.tracks) + len(results.artists) + len(results.albums)
            if total == 0:
                self.set_loading(False, f'No results for “{query}”. Try another spelling.')
            else:
                self.set_loading(
                    False,
                    f"Found {len(results.tracks)} songs, {len(results.artists)} artists, "
                    f"{len(results.albums)} albums. Enter opens · p plays song.",
                )

        self.call_from_thread(apply)


class ArtistScreen(Screen):
    def __init__(self, artist: Artist) -> None:
        super().__init__()
        self.artist = artist

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="detail-layout"):
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
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True, thread=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-refresh":
            self.run_worker(self._load(), exclusive=True, thread=True)

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        self.call_from_thread(self._set_status, "Fetching artist catalog…", loading=True)
        try:
            artist = app.client.get_artist(self.artist.id)
            tracks, _ = app.client.get_artist_tracks(self.artist.id, sort_by="popular")
            albums, _ = app.client.get_artist_albums(self.artist.id)
        except MelodifyAPIError as exc:
            self.call_from_thread(self._set_status, f"Could not load artist: {exc}", loading=False)
            return

        def apply() -> None:
            self.artist = artist
            self.query_one("#detail-title", Label).update(artist.name)
            self.query_one("#detail-meta", Label).update(artist.label())
            self.query_one("#artist-track-list", SelectableList).set_items(tracks)
            self.query_one("#artist-album-list", SelectableList).set_items(albums)
            if not tracks and not albums:
                self._set_status("This artist has no songs or albums available.", loading=False)
            else:
                self._set_status(
                    f"{len(tracks)} songs · {len(albums)} albums. Enter to open · p to play.",
                    loading=False,
                )

        self.call_from_thread(apply)


class AlbumScreen(Screen):
    def __init__(self, album: Album) -> None:
        super().__init__()
        self.album = album

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="detail-layout"):
            yield Label(self.album.title, id="detail-title")
            yield Label(self.album.label(), id="detail-meta")
            yield Static("Loading album…", id="status")
            yield LoadingIndicator(id="loading")
            with Horizontal(id="detail-actions"):
                yield Button("Back", id="btn-back", variant="default")
                yield Button("Refresh", id="btn-refresh", variant="primary")
            yield SelectableList("track", id="album-track-list", source="album")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True, thread=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-refresh":
            self.run_worker(self._load(), exclusive=True, thread=True)

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        self.call_from_thread(self._set_status, "Fetching album tracks…", loading=True)
        try:
            tracks, _ = app.client.get_album_tracks(self.album.id)
        except MelodifyAPIError as exc:
            self.call_from_thread(self._set_status, f"Could not load album: {exc}", loading=False)
            return

        def apply() -> None:
            self.query_one("#album-track-list", SelectableList).set_items(tracks)
            if not tracks:
                self._set_status("This album has no playable tracks.", loading=False)
            else:
                self._set_status(
                    f"{len(tracks)} tracks. Press Enter or p to stream one.",
                    loading=False,
                )

        self.call_from_thread(apply)


class MelodifyApp(App[None]):
    TITLE = "Melodify"
    SUB_TITLE = "terminal client"
    CSS = """
    Screen {
        background: #0b1020;
        color: #e8eefc;
    }
    #brand {
        text-style: bold;
        color: #7dd3fc;
        margin: 1 1 0 1;
        text-align: center;
        width: 100%;
    }
    #tagline {
        color: #94a3b8;
        margin: 0 1 1 1;
        text-align: center;
        width: 100%;
    }
    #search-input {
        margin: 0 1 1 1;
    }
    #home-actions {
        height: auto;
        margin: 0 1 1 1;
    }
    #status {
        margin: 0 1 1 1;
        color: #cbd5e1;
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
    #detail-actions {
        height: auto;
        margin: 0 1 1 1;
    }
    ListView {
        height: 1fr;
        border: tall #1e293b;
        margin: 0 1 1 1;
    }
    ListItem {
        padding: 0 1;
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
        self.queue = PlayQueue()
        self._last_track: Track | None = None
        self._expect_playback = False
        self._auto_advance = True

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())
        self.set_interval(0.5, self._watch_playback_end)
        missing = self.settings.missing()
        if missing:
            self.notify(
                "Missing env: " + ", ".join(missing),
                severity="error",
                timeout=8,
            )

    def on_unmount(self) -> None:
        self._expect_playback = False
        self.player.stop()
        self.client.close()

    def action_go_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def action_stop_playback(self) -> None:
        self._expect_playback = False
        position = self.player.stop()
        if self._last_track is not None and position is not None:
            self.progress.save_position(
                self._last_track.id,
                position,
                title=self._last_track.title,
                mark_last=True,
            )
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
            self.notify("Start of queue — no previous track.", severity="information", timeout=2)
            return
        self._play_track(track, start_seconds=0, from_queue=True)

    def _watch_playback_end(self) -> None:
        """Auto-advance when mpv exits after a natural end (no wrap)."""
        if not self._expect_playback or not self._auto_advance:
            return
        if self.player.is_alive:
            return
        # Player process ended while we still expected playback.
        self._expect_playback = False
        if self._last_track is not None:
            self.progress.clear_position(self._last_track.id)
        nxt = self.queue.peek_next()
        if nxt is None:
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
        label = "Paused" if paused else "Playing"
        pos = ""
        if status.position is not None:
            mins, secs = divmod(int(status.position), 60)
            pos = f" @ {mins}:{secs:02d}"
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
        if position is None:
            self.notify(f"Seek {delta:+.0f}s", timeout=1)
            return
        mins, secs = divmod(int(position), 60)
        self.notify(f"Seek → {mins}:{secs:02d}", timeout=1)
        if self._last_track is not None:
            self.progress.save_position(
                self._last_track.id,
                position,
                title=self._last_track.title,
                mark_last=True,
            )

    def _volume(self, delta: float) -> None:
        try:
            volume = self.player.volume_adjust(delta)
        except PlayerError as exc:
            self.notify(str(exc), severity="warning", timeout=3)
            return
        self.notify(f"Volume {volume:.0f}%", timeout=1)

    def action_continue_listening(self) -> None:
        self.run_worker(self._continue_worker(), exclusive=True, thread=True)

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

    def _highlighted_track(self) -> Track | None:
        context = self._highlighted_track_context()
        return context[0] if context else None

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
            self._play_worker(track, start_seconds=start_seconds),
            exclusive=True,
            thread=True,
        )

    def _continue_worker(self) -> None:
        self.call_from_thread(
            self.notify, "Fetching continue listening…", timeout=2
        )
        try:
            history = self.client.get_listening_history()
        except MelodifyAPIError as exc:
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
                "No online listening history to continue.",
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
            mins, secs = divmod(int(start_seconds), 60)
            resume_note = f" @ {mins}:{secs:02d}"
        prefix = "Continuing" if history_source else "Starting stream"
        detail = f" ({history_source})" if history_source else ""
        queue_note = f" · queue {self.queue.label()}" if self.queue.size else ""
        self.call_from_thread(
            self.notify,
            f"{prefix}{detail}: {track.title}{resume_note}{queue_note}",
            timeout=3,
        )
        try:
            # Stop previous without treating it as an auto-advance trigger.
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
            self.progress.save_position(
                track.id,
                float(start_seconds or 0),
                title=track.title,
                mark_last=True,
            )
            self.player.play(
                url,
                title=title,
                http_headers=headers,
                start_seconds=float(start_seconds or 0),
            )
            self._expect_playback = True
        except (MelodifyAPIError, PlayerError) as exc:
            self._expect_playback = False
            self.call_from_thread(
                self.notify,
                f"Play failed: {exc}",
                severity="error",
                timeout=6,
            )
            return
        backend = self.player.backend.name
        hint = "Space pause · n/b next/prev · ←/→ seek · s stop"
        self.call_from_thread(
            self.notify,
            f"Playing via {backend}: {track.title} [{self.queue.label()}] — {hint}",
            timeout=5,
        )


def run_tui(settings: Settings) -> None:
    MelodifyApp(settings).run()
