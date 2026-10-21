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
from .auth import AuthClient, AuthError, complete_login, normalize_phone
from .config import Settings, load_settings
from .history import resolve_listening_history
from .models import Album, Artist, ListeningHistory, Track
from .mpris_bridge import MprisActions
from .os_media import create_media_bridge
from .paths import data_dir
from .paging import PREFETCH_CAP, ListPager
from .player import PlayerError, StreamPlayer
from .progress import ProgressStore
from .queue import PlayQueue
from .recent import RecentStore
from .session import load_or_create_device, session_path


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
        pager: ListPager[Track] | None = None,
    ) -> None:
        self.track = track
        self.queue = list(queue or [track])
        self.source = source
        self.pager = pager
        super().__init__()


class ArtistSelected(Message):
    def __init__(self, artist: Artist) -> None:
        self.artist = artist
        super().__init__()


class AlbumSelected(Message):
    def __init__(self, album: Album) -> None:
        self.album = album
        super().__init__()


class LoadMoreRequested(Message):
    """Ask the app to fetch the next Melodify page for this list."""

    def __init__(self, list_view: SelectableList) -> None:
        self.list_view = list_view
        super().__init__()


class SelectableList(ListView):
    """ListView that posts domain selection messages and supports Melodify paging."""

    BINDINGS = [
        *ListView.BINDINGS,
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Top", show=False),
        Binding("end", "scroll_end", "Bottom", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("m", "load_more", "More", show=False),
    ]

    def __init__(self, kind: str, *, source: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.kind = kind
        self.source = source
        self._items: list[Artist | Album | Track] = []
        self.pager: ListPager[Any] | None = None
        self._pending_load = False

    def set_items(
        self,
        items: list[Artist | Album | Track],
        *,
        focus: bool = False,
        end: bool = True,
    ) -> None:
        """Replace items with a static (non-paged) list."""
        self.pager = ListPager(fetch=lambda _offset: ([], True), items=list(items), end=end)
        self._items = list(items)
        self._rebuild(focus=focus, reset_index=True)

    def set_pager(self, pager: ListPager[Any], *, focus: bool = False) -> None:
        self.pager = pager
        self._items = list(pager.items)
        self._pending_load = False
        self._rebuild(focus=focus, reset_index=True)

    def apply_pager_state(self, *, keep_index: bool = True) -> None:
        if self.pager is not None:
            self._items = list(self.pager.items)
        self._pending_load = False
        old = self.index
        self._rebuild(focus=False, reset_index=False)
        if keep_index and old is not None and self._items:
            # Stay on content; allow landing on the load-more sentinel.
            max_idx = self._row_count() - 1
            self.index = max(0, min(old, max_idx))

    def track_items(self) -> list[Track]:
        return [item for item in self._items if isinstance(item, Track)]

    def track_pager(self) -> ListPager[Track] | None:
        if self.kind != "track" or self.pager is None:
            return None
        return self.pager  # type: ignore[return-value]

    def has_more(self) -> bool:
        return bool(self.pager and self.pager.has_more)

    def _row_count(self) -> int:
        n = len(self._items)
        if self.pager is not None and not self.pager.end:
            n += 1  # load-more sentinel row
        return n if n > 0 else 1

    def _rebuild(self, *, focus: bool, reset_index: bool) -> None:
        self.clear()
        if not self._items:
            if self.pager is not None and not self.pager.end:
                label = "Loading…" if self.pager.loading else "── Load more (m) ──"
                self.append(ListItem(Label(label)))
                self.index = 0
            else:
                self.append(ListItem(Label("Nothing here yet.")))
                self.index = None
            if focus:
                self.focus()
            return
        for item in self._items:
            self.append(ListItem(Label(item.label())))
        if self.pager is not None and not self.pager.end:
            label = (
                "── Loading more… ──"
                if self.pager.loading
                else "── Load more (m / PgDn at end) ──"
            )
            self.append(ListItem(Label(label)))
        if reset_index:
            self.index = 0
            self.scroll_home(animate=False)
        if focus:
            self.focus()

    def _request_load_more(self) -> None:
        if not self.has_more() or self._pending_load:
            return
        self._pending_load = True
        self.post_message(LoadMoreRequested(self))

    def action_load_more(self) -> None:
        self._request_load_more()

    def action_page_down(self) -> None:
        if not self._items and not self.has_more():
            return
        step = max(1, self.size.height - 1)
        current = self.index if self.index is not None else 0
        last = self._row_count() - 1
        nxt = min(last, current + step)
        self.index = nxt
        # Only auto-fetch when the highlight lands on the Load-more sentinel.
        if nxt >= len(self._items):
            self._request_load_more()

    def action_page_up(self) -> None:
        if not self._items:
            return
        step = max(1, self.size.height - 1)
        current = self.index if self.index is not None else 0
        self.index = max(0, current - step)

    def action_scroll_home(self) -> None:
        if self._items or self.has_more():
            self.index = 0

    def action_scroll_end(self) -> None:
        if self._items or self.has_more():
            self.index = self._row_count() - 1
            self._request_load_more()

    def watch_index(self, old_index: int | None, new_index: int | None) -> None:
        # Keep ListView highlight + scroll-into-view, then maybe page.
        super().watch_index(old_index, new_index)
        if new_index is None or not self.has_more():
            return
        # Auto-fetch only on the Load-more sentinel (avoids chaining every page).
        if new_index >= len(self._items):
            self._request_load_more()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or index < 0:
            return
        if index >= len(self._items):
            # Sentinel "Load more" row.
            self._request_load_more()
            return
        item = self._items[index]
        if self.kind == "track" and isinstance(item, Track):
            self.post_message(
                TrackSelected(
                    item,
                    queue=self.track_items(),
                    source=self.source or self.id or "list",
                    pager=self.track_pager(),
                )
            )
        elif self.kind == "artist" and isinstance(item, Artist):
            self.post_message(ArtistSelected(item))
        elif self.kind == "album" and isinstance(item, Album):
            self.post_message(AlbumSelected(item))


class PlayerChrome(Vertical):
    """Now-playing + progress + quiet compact Up next (not rebuilt on IPC ticks)."""

    # Compact default; expanded shows more upcoming. Toggle with `u`.
    COMPACT_UPCOMING = 3
    EXPANDED_UPCOMING = 12

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
    #np-upnext {
        height: auto;
        max-height: 14;
        color: #cbd5e1;
        padding: 0 0 0 0;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._expanded = False
        # Dirty-check caches so the 0.2s IPC tick only touches time/progress.
        self._last_title: str | None = None
        self._last_meta: str | None = None
        self._last_time: str | None = None
        self._last_bar: int | None = None
        self._last_queue_sig: tuple[Any, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Label("Not playing — pick a song to start the queue.", id="np-title")
        yield Label("Space pause · n/b next/prev · ←/→ seek · -/= vol · u Up next", id="np-meta")
        yield Label("--:-- / --:--", id="np-time")
        yield ProgressBar(id="np-bar", total=1000, show_eta=False, show_percentage=False)
        yield Label("Up next", id="np-queue-label")
        yield Static(
            "Queue empty — play from search, artist, album, or last.",
            id="np-upnext",
        )

    def toggle_expanded(self) -> bool:
        """Flip compact ↔ expanded Up next. Returns new expanded state."""
        self._expanded = not self._expanded
        # Force queue redraw on next refresh.
        self._last_queue_sig = None
        return self._expanded

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
            self._set_title("Not playing — pick a song to start the queue.")
            self._set_meta("Space pause · n/b next/prev · ←/→ seek · -/= vol · u Up next")
            self._set_time("--:-- / --:--")
            self._set_bar(0)
            self._refresh_up_next(queue)
            return

        state = "Paused" if paused else ("Playing" if playing else "Stopped")
        vol = f" · vol {volume:.0f}%" if volume is not None else ""
        self._set_title(f"{state}: {title.label()}")
        self._set_meta(
            f"{queue.label()}{vol} · Space pause · n/b next/prev · u Up next"
        )
        self._set_time(f"{_fmt_time(position)} / {_fmt_time(duration)}")
        if duration and duration > 0 and position is not None:
            self._set_bar(min(1000, int((position / duration) * 1000)))
        else:
            self._set_bar(0)

        # Queue panel: only when index / contents / expand mode change — never
        # clear+rebuild a ListView on every progress tick (that caused the blink).
        self._refresh_up_next(queue)

    def _set_title(self, text: str) -> None:
        if text == self._last_title:
            return
        self._last_title = text
        self.query_one("#np-title", Label).update(text)

    def _set_meta(self, text: str) -> None:
        if text == self._last_meta:
            return
        self._last_meta = text
        self.query_one("#np-meta", Label).update(text)

    def _set_time(self, text: str) -> None:
        if text == self._last_time:
            return
        self._last_time = text
        self.query_one("#np-time", Label).update(text)

    def _set_bar(self, progress: int) -> None:
        if progress == self._last_bar:
            return
        self._last_bar = progress
        self.query_one("#np-bar", ProgressBar).update(progress=progress)

    def _up_next_window(self, queue: PlayQueue) -> list[tuple[int, Track, bool]]:
        """Rows to show: current (▶) plus a few upcoming. No past tracks."""
        if not queue.tracks or queue.current is None:
            return []
        upcoming = self.EXPANDED_UPCOMING if self._expanded else self.COMPACT_UPCOMING
        rows: list[tuple[int, Track, bool]] = [
            (queue.index, queue.tracks[queue.index], True)
        ]
        for i in range(queue.index + 1, min(len(queue.tracks), queue.index + 1 + upcoming)):
            rows.append((i, queue.tracks[i], False))
        return rows

    def _refresh_up_next(self, queue: PlayQueue) -> None:
        rows = self._up_next_window(queue)
        sig: tuple[Any, ...] = (
            queue.index,
            len(queue.tracks),
            queue.source,
            self._expanded,
            tuple((i, t.id) for i, t, _ in rows),
        )
        if sig == self._last_queue_sig:
            return
        self._last_queue_sig = sig

        mode = "expanded" if self._expanded else "compact"
        remaining = queue.remaining()
        if not rows:
            label = "Up next"
            body = "Queue empty — play from search, artist, album, or last."
        else:
            more = max(0, remaining - (len(rows) - 1))
            extra = f" · +{more} more" if more else ""
            label = f"Up next · {queue.label()} · {mode}{extra} · u toggle"
            lines: list[str] = []
            for i, track, is_current in rows:
                marker = "▶" if is_current else " "
                lines.append(f"{marker} {i + 1}. {track.label()}")
            body = "\n".join(lines)

        self.query_one("#np-queue-label", Label).update(label)
        self.query_one("#np-upnext", Static).update(body)


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
                    yield Button("Load more", id="btn-load-more", variant="default")
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
        elif event.button.id == "btn-load-more":
            app = self.app
            assert isinstance(app, MelodifyApp)
            app.action_load_more()

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
            client = app.client
            if history.source == "local recently played" or history.end:
                pager: ListPager[Track] = ListPager(
                    fetch=lambda _offset: ([], True),
                    items=list(history.tracks),
                    end=True,
                    label=history.describe_source(),
                )
            else:

                def fetch_online(offset: int) -> tuple[list[Track], bool]:
                    page = client.get_listening_history(offset=offset)
                    return page.tracks, page.end

                pager = ListPager(
                    fetch=fetch_online,
                    items=list(history.tracks),
                    end=history.end,
                    label=history.describe_source(),
                )
            last_list.set_pager(pager, focus=not silent)
            try:
                tabs = self.query_one("#results-tabs", TabbedContent)
                tabs.active = "tab-last"
            except Exception:
                pass
            if not history.tracks:
                self.set_loading(
                    False,
                    "No local recently-played tracks yet (and online history is empty). "
                    f"Play something first — Melodify saves them under {data_dir()}/.",
                )
            else:
                more = (
                    ""
                    if history.end
                    else f" · loading up to {PREFETCH_CAP}…"
                )
                self.set_loading(
                    False,
                    f"{len(history.tracks)} tracks from {history.describe_source()}{more}. "
                    "↑↓/PgUp/PgDn scroll · Enter plays · c continues the first.",
                )
            app.schedule_prefetch(last_list)

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

        client = app.client

        def fetch_tracks(offset: int) -> tuple[list[Track], bool]:
            page = client.search(query, search_type=1, offset=offset)
            return page.tracks, page.end

        def fetch_artists(offset: int) -> tuple[list[Artist], bool]:
            page = client.search(query, search_type=2, offset=offset)
            return page.artists, page.end

        def fetch_albums(offset: int) -> tuple[list[Album], bool]:
            page = client.search(query, search_type=3, offset=offset)
            return page.albums, page.end

        def apply() -> None:
            tracks = self.query_one("#track-list", SelectableList)
            # Type-6 "end" is unreliable for continued type-1/2/3 paging — allow
            # prefetch to discover the real end (or stop at PREFETCH_CAP).
            tracks.set_pager(
                ListPager(
                    fetch=fetch_tracks,
                    items=list(results.tracks),
                    end=not bool(results.tracks),
                    label=f'search “{query}” songs',
                ),
                focus=bool(results.tracks),
            )
            artists = self.query_one("#artist-list", SelectableList)
            artists.set_pager(
                ListPager(
                    fetch=fetch_artists,
                    items=list(results.artists),
                    end=not bool(results.artists),
                    label=f'search “{query}” artists',
                )
            )
            albums = self.query_one("#album-list", SelectableList)
            albums.set_pager(
                ListPager(
                    fetch=fetch_albums,
                    items=list(results.albums),
                    end=not bool(results.albums),
                    label=f'search “{query}” albums',
                )
            )
            total = len(results.tracks) + len(results.artists) + len(results.albums)
            if total == 0:
                self.set_loading(False, f'No results for “{query}”. Try another spelling.')
            else:
                self.set_loading(
                    False,
                    f"Found {len(results.tracks)} songs, {len(results.artists)} artists, "
                    f"{len(results.albums)} albums · loading up to {PREFETCH_CAP}…. "
                    "↑↓/PgUp/PgDn scroll · Enter opens · p plays.",
                )
            try:
                tabs = self.query_one("#results-tabs", TabbedContent)
                tabs.active = "tab-tracks"
            except Exception:
                pass
            app.schedule_prefetch(tracks)
            app.schedule_prefetch(artists)
            app.schedule_prefetch(albums)

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
                    yield Button("Load more", id="btn-load-more", variant="default")
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
        elif event.button.id == "btn-load-more":
            app = self.app
            assert isinstance(app, MelodifyApp)
            app.action_load_more()

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        _call_ui(self, self._set_status, "Fetching artist catalog…", loading=True)
        artist_id = self.artist.id
        try:
            artist = app.client.get_artist(artist_id)
            tracks, tracks_end = app.client.get_artist_tracks(
                artist_id, sort_by="popular", offset=0
            )
            albums, albums_end = app.client.get_artist_albums(artist_id, offset=0)
        except MelodifyAPIError as exc:
            _call_ui(self, self._set_status, f"Could not load artist: {exc}", loading=False)
            return

        client = app.client

        def fetch_tracks(offset: int) -> tuple[list[Track], bool]:
            return client.get_artist_tracks(artist_id, offset=offset, sort_by="popular")

        def fetch_albums(offset: int) -> tuple[list[Album], bool]:
            return client.get_artist_albums(artist_id, offset=offset)

        def apply() -> None:
            self.artist = artist
            self.query_one("#detail-title", Label).update(artist.name)
            self.query_one("#detail-meta", Label).update(artist.label())
            track_list = self.query_one("#artist-track-list", SelectableList)
            track_list.set_pager(
                ListPager(
                    fetch=fetch_tracks,
                    items=list(tracks),
                    end=tracks_end,
                    label=f"artist {artist_id} tracks",
                ),
                focus=bool(tracks),
            )
            album_list = self.query_one("#artist-album-list", SelectableList)
            album_list.set_pager(
                ListPager(
                    fetch=fetch_albums,
                    items=list(albums),
                    end=albums_end,
                    label=f"artist {artist_id} albums",
                )
            )
            if not tracks and not albums:
                self._set_status("This artist has no songs or albums available.", loading=False)
            else:
                more = (
                    ""
                    if tracks_end and albums_end
                    else f" · loading up to {PREFETCH_CAP}…"
                )
                self._set_status(
                    f"{len(tracks)} songs · {len(albums)} albums{more}. "
                    "↑↓/PgUp/PgDn scroll · Enter plays · fills queue.",
                    loading=False,
                )
            app.schedule_prefetch(track_list)
            app.schedule_prefetch(album_list)

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
                    yield Button("Load more", id="btn-load-more", variant="default")
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
        elif event.button.id == "btn-load-more":
            app = self.app
            assert isinstance(app, MelodifyApp)
            app.action_load_more()

    def _set_status(self, text: str, *, loading: bool = False) -> None:
        self.query_one("#status", Static).update(text)
        self.query_one("#loading", LoadingIndicator).display = loading

    def _load(self) -> None:
        app = self.app
        assert isinstance(app, MelodifyApp)
        _call_ui(self, self._set_status, "Fetching album tracks…", loading=True)
        album_id = self.album.id
        try:
            tracks, tracks_end = app.client.get_album_tracks(album_id, offset=0)
        except MelodifyAPIError as exc:
            _call_ui(self, self._set_status, f"Could not load album: {exc}", loading=False)
            return

        client = app.client

        def fetch_tracks(offset: int) -> tuple[list[Track], bool]:
            return client.get_album_tracks(album_id, offset=offset)

        def apply() -> None:
            track_list = self.query_one("#album-track-list", SelectableList)
            track_list.set_pager(
                ListPager(
                    fetch=fetch_tracks,
                    items=list(tracks),
                    end=tracks_end,
                    label=f"album {album_id} tracks",
                ),
                focus=bool(tracks),
            )
            if not tracks:
                self._set_status("This album has no playable tracks.", loading=False)
            else:
                more = "" if tracks_end else f" · loading up to {PREFETCH_CAP}…"
                self._set_status(
                    f"{len(tracks)} tracks{more}. ↑↓/PgUp/PgDn scroll · Enter or p plays.",
                    loading=False,
                )
            app.schedule_prefetch(track_list)

        _call_ui(self, apply)


class LoginScreen(Screen):
    """Phone + OTP when no session is available."""

    # priority=True so focused Inputs don't swallow q / escape / ctrl+c.
    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=True, priority=True),
        Binding("escape", "quit_app", "Quit", show=True, priority=True),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    LoginScreen {
        align: center middle;
        background: #0f172a;
    }
    #login-box {
        width: 64;
        max-width: 90%;
        height: auto;
        border: tall #334155;
        background: #111827;
        padding: 1 2;
    }
    #login-title {
        text-style: bold;
        color: #fde68a;
        height: 1;
        margin-bottom: 1;
    }
    #login-help {
        color: #94a3b8;
        margin-bottom: 1;
    }
    #login-status {
        color: #7dd3fc;
        margin: 1 0;
        min-height: 2;
    }
    #login-phone, #login-code, #login-name {
        margin: 0 0 1 0;
    }
    #login-actions {
        height: 3;
        margin-top: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._phone: str = ""
        self._awaiting_code = False

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Label("Melodify login", id="login-title")
            yield Static(
                "Sign in with your Melodify phone number (SMS code).\n"
                "Or run `melodify login` in a terminal.\n"
                f"Session file: {session_path()}\n"
                "Quit: q · Esc · Ctrl+C · or the Quit button.",
                id="login-help",
            )
            yield Input(placeholder="Phone 09xxxxxxxxx", id="login-phone")
            yield Input(placeholder="SMS code (after send)", id="login-code")
            yield Input(
                placeholder="Display name (new accounts only)",
                id="login-name",
            )
            with Horizontal(id="login-actions"):
                yield Button("Send code", id="btn-send-code", variant="primary")
                yield Button("Verify & save", id="btn-verify", variant="success")
                yield Button("Quit", id="btn-quit", variant="error")
            yield Static("Enter your phone, then Send code.", id="login-status")

    def on_mount(self) -> None:
        self.query_one("#login-phone", Input).focus()

    def action_quit_app(self) -> None:
        self.app.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-quit":
            self.app.exit()
            return
        if event.button.id == "btn-send-code":
            self._send_code()
        elif event.button.id == "btn-verify":
            self._verify()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "login-phone":
            self._send_code()
        elif event.input.id in {"login-code", "login-name"}:
            self._verify()

    def _set_status(self, text: str) -> None:
        self.query_one("#login-status", Static).update(text)

    def _send_code(self) -> None:
        phone_raw = self.query_one("#login-phone", Input).value.strip()
        self.run_worker(
            partial(self._send_code_worker, phone_raw),
            exclusive=True,
            thread=True,
        )

    def _send_code_worker(self, phone_raw: str) -> None:
        try:
            phone = normalize_phone(phone_raw)
            device = load_or_create_device(default_name=self.app.settings.device_name)
            with AuthClient(device, platform=self.app.settings.platform) as client:
                msg = client.request_otp(phone)
            self._phone = phone
            self._awaiting_code = True
            _call_ui(self, self._set_status, f"{msg}\nPhone: {phone}")
            _call_ui(self, self.query_one("#login-code", Input).focus)
        except AuthError as exc:
            _call_ui(self, self._set_status, str(exc))
        except Exception as exc:  # noqa: BLE001
            _call_ui(
                self,
                self._set_status,
                f"Could not send SMS ({exc}). Try again.",
            )

    def _verify(self) -> None:
        phone_raw = self.query_one("#login-phone", Input).value.strip()
        code = self.query_one("#login-code", Input).value.strip()
        name = self.query_one("#login-name", Input).value.strip() or None
        phone = self._phone or phone_raw
        self.run_worker(
            partial(self._verify_worker, phone, code, name),
            exclusive=True,
            thread=True,
        )

    def _verify_worker(self, phone: str, code: str, name: str | None) -> None:
        try:
            session = complete_login(phone=phone, code=code, full_name=name)
            _call_ui(self, self.app.apply_session, session)
        except AuthError as exc:
            _call_ui(self, self._set_status, str(exc))
        except Exception as exc:  # noqa: BLE001
            _call_ui(
                self,
                self._set_status,
                f"Login failed ({exc}). Try again.",
            )


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
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("u", "toggle_up_next", "Up next"),
        Binding("p", "play_highlighted", "Play"),
        Binding("c", "continue_listening", "Continue"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "play_next", "Next"),
        Binding("b", "play_prev", "Prev"),
        Binding(">", "play_next", "Next", show=False),
        Binding("<", "play_prev", "Prev", show=False),
        Binding("s", "stop_playback", "Stop"),
        Binding("m", "load_more", "More"),
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
        self.media = create_media_bridge()
        # Alias kept so existing notify/sync call sites stay readable.
        self.mpris = self.media
        self._last_track: Track | None = None
        self._expect_playback = False
        self._saw_alive = False
        self._playback_started_at = 0.0
        self._auto_advance = True
        self._prefetch_inflight = False
        # Bumped on every play request so stale exclusive thread-workers cannot
        # restart mpv after a slow /listens call (~30s restart bug).
        self._play_generation = 0
        self._last_select_id: int | None = None
        self._last_select_at = 0.0

    def on_mount(self) -> None:
        # Faster than 0.5s so EOF → next track feels immediate.
        self.set_interval(0.2, self._on_player_tick)
        if self.settings.authenticated:
            self.push_screen(SearchScreen())
            self._start_os_media()
        else:
            self.push_screen(LoginScreen())
            self.notify(
                "Not logged in — use the form or run `melodify login`.",
                severity="warning",
                timeout=6,
            )

    def apply_session(self, session: Any) -> None:
        """Called after LoginScreen succeeds — swap client + enter the app."""
        from .session import Session as _Session

        if not isinstance(session, _Session):
            return
        self.settings = load_settings()
        # Prefer the just-saved session fields even if an empty .env was loaded.
        self.settings = Settings(
            authorization=session.authorization,
            user_id=session.user_id,
            device_id=session.device_id,
            device_token=session.device_token,
            api_base=self.settings.api_base,
            origin=self.settings.origin,
            app_version=self.settings.app_version,
            pwa_version=self.settings.pwa_version,
            platform=session.platform or self.settings.platform,
            device_name=session.device_name or self.settings.device_name,
            stream_quality=self.settings.stream_quality,
            player=self.settings.player,
            auth_source="session",
        )
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.client = MelodifyClient(self.settings)
        # Replace login screen with the main UI.
        self.pop_screen()
        self.push_screen(SearchScreen())
        self._start_os_media()
        self.notify(
            f"Logged in as {session.full_name or session.user_id}",
            timeout=4,
        )

    def on_unmount(self) -> None:
        self._expect_playback = False
        self._persist_current_position(mark_recent=True)
        self.media.stop()
        self.player.stop()
        self.client.close()

    def _start_os_media(self) -> None:
        app = self

        def _call(action: str) -> None:
            # OS media thread → Textual thread.
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
                    self.media.notify(seek=True)
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
                    self.media.notify(volume=True)
                except PlayerError as exc:
                    self.notify(str(exc), severity="warning", timeout=3)

            try:
                self.call_from_thread(run)
            except RuntimeError:
                run()

        self.media.attach(
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

        if self.media.start():
            from .paths import is_macos

            msg = (
                "OS media controls on — Control Center / media keys."
                if is_macos()
                else "OS media controls on — GNOME/KDE keys & playerctl (-p Melodify)."
            )
            self.notify(msg, timeout=4)
        elif getattr(self.media, "last_error", None):
            self.notify(
                f"OS media controls off: {self.media.last_error}",
                severity="warning",
                timeout=8,
            )

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
            elif self.player.is_alive:
                self.player.resume()
            elif self._last_track is not None:
                # OS media stacks sometimes echo Play right after we publish
                # metadata; ignore that while a start is in flight.
                if (time.time() - self._playback_started_at) < 2.5:
                    return
                self._play_track(self._last_track, start_seconds=0, from_queue=True)
                return
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

    def action_toggle_up_next(self) -> None:
        try:
            chrome = self.screen.query_one("#player-chrome", PlayerChrome)
        except Exception:
            return
        expanded = chrome.toggle_expanded()
        self.refresh_player_chrome()
        self.notify(
            "Up next: expanded" if expanded else "Up next: compact",
            timeout=1.5,
        )

    def _on_player_tick(self) -> None:
        # Progress/time may update every tick; Up next only redraws when the
        # queue signature changes (see PlayerChrome._refresh_up_next).
        self.refresh_player_chrome()
        self._maybe_prefetch_queue()
        self._watch_playback_end()

    def schedule_prefetch(self, lv: SelectableList) -> None:
        """Background-fetch pages until end or PREFETCH_CAP (500)."""
        pager = lv.pager
        if pager is None or pager.end or len(pager.items) >= PREFETCH_CAP:
            return
        if pager.prefetching:
            return
        self.run_worker(
            partial(self._prefetch_list_worker, lv),
            exclusive=False,
            thread=True,
        )

    def _prefetch_list_worker(self, lv: SelectableList) -> None:
        pager = lv.pager
        if pager is None or pager.end or len(pager.items) >= PREFETCH_CAP:
            return

        def on_progress(count: int, ended: bool) -> None:
            def apply() -> None:
                lv.apply_pager_state(keep_index=True)
                if lv.kind == "track" and self.queue.pager is pager:
                    self.queue.extend_unique(
                        [t for t in pager.items if isinstance(t, Track)]
                    )
                    self.refresh_player_chrome()
                try:
                    status = self.screen.query_one("#status", Static)
                    suffix = ""
                    if ended:
                        suffix = " · done"
                    elif count >= PREFETCH_CAP:
                        suffix = f" · cap {PREFETCH_CAP} (m for more)"
                    else:
                        suffix = "…"
                    label = pager.label or "list"
                    status.update(f"Loaded {count} · {label}{suffix}")
                except Exception:
                    pass

            self.call_from_thread(apply)

        try:
            pager.prefetch_until(cap=PREFETCH_CAP, on_progress=on_progress)
        except MelodifyAPIError as exc:
            self.call_from_thread(
                self.notify,
                f"Prefetch stopped: {exc}",
                severity="warning",
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify,
                f"Prefetch stopped: {exc}",
                severity="warning",
                timeout=5,
            )

        def done() -> None:
            lv.apply_pager_state(keep_index=True)
            if lv.kind == "track" and self.queue.pager is pager:
                self.queue.extend_unique(
                    [t for t in pager.items if isinstance(t, Track)]
                )
                self.refresh_player_chrome()
            n = len(lv._items)
            if pager.end:
                detail = f"Loaded {n} · complete"
            elif n >= PREFETCH_CAP:
                detail = f"Loaded {n} (cap {PREFETCH_CAP}) · m / Load more for more"
            else:
                detail = f"Loaded {n}"
            try:
                self.screen.query_one("#status", Static).update(
                    f"{detail} · {pager.label or 'list'}"
                )
            except Exception:
                pass
            self.notify(detail, timeout=3)

        self.call_from_thread(done)

    def action_load_more(self) -> None:
        target = self._active_selectable_list()
        if target is None:
            self.notify("Focus a list first, then press m / Load more.", timeout=3)
            return
        if not target.has_more():
            self.notify("No more pages for this list.", timeout=2)
            return
        target._request_load_more()

    def on_load_more_requested(self, event: LoadMoreRequested) -> None:
        event.stop()
        lv = event.list_view
        if lv.pager is None or not lv.pager.has_more:
            lv._pending_load = False
            return
        self.run_worker(
            partial(self._load_more_worker, lv),
            exclusive=False,
            thread=True,
        )

    def _load_more_worker(self, lv: SelectableList) -> None:
        pager = lv.pager
        if pager is None:
            return
        try:
            added = pager.load_more()
        except MelodifyAPIError as exc:
            lv._pending_load = False
            self.call_from_thread(
                self.notify,
                f"Load more failed: {exc}",
                severity="error",
                timeout=5,
            )
            return
        except Exception as exc:  # noqa: BLE001
            lv._pending_load = False
            self.call_from_thread(
                self.notify,
                f"Load more failed: {exc}",
                severity="error",
                timeout=5,
            )
            return

        def apply() -> None:
            lv.apply_pager_state(keep_index=True)
            if lv.kind == "track" and self.queue.pager is pager and added:
                self.queue.extend_unique([t for t in added if isinstance(t, Track)])
                self.refresh_player_chrome()
            if added:
                self.notify(
                    f"Loaded {len(added)} more · {len(lv._items)} total"
                    + ("" if pager.end else " · more available"),
                    timeout=2,
                )
            else:
                self.notify("End of list.", timeout=2)

        self.call_from_thread(apply)

    def _active_selectable_list(self) -> SelectableList | None:
        focused = self.focused
        if isinstance(focused, SelectableList):
            return focused
        screen = self.screen
        # Prefer the visible tab's primary list.
        candidates = (
            "#track-list",
            "#artist-list",
            "#album-list",
            "#last-list",
            "#artist-track-list",
            "#artist-album-list",
            "#album-track-list",
        )
        for list_id in candidates:
            try:
                widget = screen.query_one(list_id, SelectableList)
            except Exception:
                continue
            if widget.has_more():
                return widget
        for list_id in candidates:
            try:
                return screen.query_one(list_id, SelectableList)
            except Exception:
                continue
        return None

    def _attach_queue_pager(self, pager: ListPager[Track] | None) -> None:
        self.queue.pager = pager

    def _maybe_prefetch_queue(self) -> None:
        pager = self.queue.pager
        if pager is None or not pager.has_more or self._prefetch_inflight:
            return
        if self.queue.remaining() > 3:
            return
        self._prefetch_inflight = True
        self.run_worker(self._prefetch_queue_worker, exclusive=False, thread=True)

    def _prefetch_queue_worker(self) -> None:
        pager = self.queue.pager
        try:
            if pager is None or not pager.has_more:
                return
            added = pager.load_more()
            if not added:
                return

            def apply() -> None:
                self.queue.extend_unique([t for t in added if isinstance(t, Track)])
                # Refresh any on-screen list sharing this pager.
                for lv in self.screen.query(SelectableList):
                    if lv.pager is pager:
                        lv.apply_pager_state(keep_index=True)
                self.refresh_player_chrome()

            self.call_from_thread(apply)
        except Exception:
            pass
        finally:
            self._prefetch_inflight = False
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
        alive = self.player.is_alive
        eof = self.player.reached_eof() if alive or self._saw_alive else False
        if alive and not eof:
            self._saw_alive = True
            return
        # Ignore the brief window before mpv is up, otherwise we'd skip tracks.
        if not self._saw_alive or (time.time() - self._playback_started_at) < 1.0:
            return
        if alive and eof:
            # Don't wait for HTTP keep-alive to tear down the process.
            try:
                self.player.stop()
            except Exception:  # noqa: BLE001
                pass
        elif alive:
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
        track, queue_tracks, source, pager = context
        self.queue.load(queue_tracks, start=track, source=source)
        self._attach_queue_pager(pager)
        self._play_track(track, start_seconds=0, from_queue=True)

    def on_track_selected(self, event: TrackSelected) -> None:
        # Debounce duplicate Enter/Selected messages for the same track.
        now = time.time()
        if (
            self._last_select_id == event.track.id
            and (now - self._last_select_at) < 0.6
        ):
            return
        self._last_select_id = event.track.id
        self._last_select_at = now
        self.queue.load(event.queue, start=event.track, source=event.source or "list")
        self._attach_queue_pager(event.pager)
        self._play_track(event.track, start_seconds=0, from_queue=True)

    def on_artist_selected(self, event: ArtistSelected) -> None:
        self.push_screen(ArtistScreen(event.artist))

    def on_album_selected(self, event: AlbumSelected) -> None:
        self.push_screen(AlbumScreen(event.album))

    def _highlighted_track_context(
        self,
    ) -> tuple[Track, list[Track], str, ListPager[Track] | None] | None:
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
                return (
                    item,
                    widget.track_items(),
                    widget.source or list_id,
                    widget.track_pager(),
                )
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
        self._play_generation += 1
        generation = self._play_generation
        # Stop expecting the previous track so its end-watcher cannot race us.
        self._expect_playback = False
        self._saw_alive = False
        self.run_worker(
            partial(
                self._play_worker,
                track,
                start_seconds=start_seconds,
                generation=generation,
            ),
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
                f"Nothing to continue — play something first (saved under {data_dir()}/).",
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
        if history.source != "local recently played" and not history.end:
            client = self.client

            def fetch_online(offset: int) -> tuple[list[Track], bool]:
                page = client.get_listening_history(offset=offset)
                return page.tracks, page.end

            self._attach_queue_pager(
                ListPager(
                    fetch=fetch_online,
                    items=list(history.tracks),
                    end=history.end,
                    label=history.describe_source(),
                )
            )
        self._last_track = track
        start = self.progress.resolve_start_seconds(
            track.id,
            api_progress=track.progress_seconds,
            duration=track.duration,
        )
        self._play_generation += 1
        self._play_worker(
            track,
            start_seconds=start,
            history_source=history.describe_source(),
            generation=self._play_generation,
        )

    def _play_worker(
        self,
        track: Track,
        *,
        start_seconds: float | None = None,
        history_source: str | None = None,
        generation: int = 0,
    ) -> None:
        def _stale() -> bool:
            return generation != 0 and generation != self._play_generation

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
        if _stale():
            return
        self.call_from_thread(
            self.notify,
            f"{prefix}{detail}: {track.title}{resume_note} · {self.queue.label()}",
            timeout=3,
        )
        try:
            self._expect_playback = False
            url = self.client.build_play_url(track)
            if _stale():
                return
            headers = {
                "authorization": self.settings.authorization,
                "user-id": self.settings.user_id,
                "device-id": self.settings.device_id,
                "device-token": self.settings.device_token,
                "Origin": self.settings.origin,
                "Referer": f"{self.settings.origin}/",
            }
            seek = float(start_seconds or 0)
            self.progress.save_position(
                track.id,
                seek,
                title=track.title,
                mark_last=True,
            )
            self.recent.upsert(track, position_seconds=seek)
            if _stale():
                return
            # Start audio first — never block playback on /listens.
            self.player.play(
                url,
                title=title,
                http_headers=headers,
                start_seconds=seek,
            )
            if _stale():
                try:
                    self.player.stop()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._saw_alive = False
            self._playback_started_at = time.time()
            self._expect_playback = True
        except (MelodifyAPIError, PlayerError) as exc:
            if _stale():
                return
            self._expect_playback = False
            self._saw_alive = False
            self.call_from_thread(
                self.notify,
                f"Play failed: {exc}",
                severity="error",
                timeout=6,
            )
            return
        # Fire-and-forget listen accounting (slow/hanging API must not re-enter play).
        try:
            self.client.record_listen(track)
        except Exception:  # noqa: BLE001
            pass
        if _stale():
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
