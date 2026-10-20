from __future__ import annotations

import argparse
import sys

from . import __version__
from .api import MelodifyAPIError, MelodifyClient
from .config import load_settings
from .models import Track
from .controls import interactive_playback_loop
from .player import PlayerError, StreamPlayer, detect_player
from .progress import ProgressStore
from .queue import PlayQueue
from .tui import run_tui


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="melodify",
        description="Ubuntu terminal music client for Melodify (search, browse, stream).",
    )
    parser.add_argument("--version", action="version", version=f"melodify {__version__}")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: ./.env)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Launch the interactive TUI (default).")

    p_search = sub.add_parser("search", help="One-shot search in the terminal.")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument(
        "--type",
        dest="search_type",
        type=int,
        default=6,
        help="Search type: 6=all (default), 1=tracks, 2=artists, 3=albums",
    )

    p_play = sub.add_parser("play", help="Search and stream the first matching track.")
    p_play.add_argument("query", help="Track search query")
    p_play.add_argument(
        "--quality",
        default=None,
        help="Stream quality: 320, 160, or 96 (default from env)",
    )
    p_play.add_argument(
        "--from",
        dest="start_seconds",
        type=float,
        default=None,
        help="Start at this many seconds (overrides saved progress)",
    )

    p_artist = sub.add_parser("artist", help="Show artist profile + top tracks.")
    p_artist.add_argument("artist_id", type=int)

    p_album = sub.add_parser("album", help="List album tracks.")
    p_album.add_argument("album_id", type=int)

    p_last = sub.add_parser(
        "last",
        help="List online listening history (recently listened / most listened).",
    )
    p_last.add_argument(
        "--limit",
        type=int,
        default=25,
        help="How many tracks to show (default: 25)",
    )

    p_continue = sub.add_parser(
        "continue",
        help="Resume the top online listening-history track (continue listening).",
    )
    p_continue.add_argument(
        "--from",
        dest="start_seconds",
        type=float,
        default=None,
        help="Force start position in seconds (default: saved progress or 0)",
    )
    p_continue.add_argument(
        "--quality",
        default=None,
        help="Stream quality: 320, 160, or 96 (default from env)",
    )

    args = parser.parse_args(argv)
    settings = load_settings(args.env_file)
    missing = settings.missing()
    if missing:
        print(
            "Missing required environment variables:\n  - "
            + "\n  - ".join(missing)
            + "\n\nCopy .env.example to .env and fill in your Melodify session values.",
            file=sys.stderr,
        )
        return 2

    command = args.command or "tui"
    progress = ProgressStore()

    if command == "tui":
        try:
            detect_player(settings.player)
        except PlayerError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        run_tui(settings)
        return 0

    with MelodifyClient(settings) as client:
        try:
            if command == "search":
                results = client.search(args.query, search_type=args.search_type)
                _print_search(results)
                return 0
            if command == "artist":
                artist = client.get_artist(args.artist_id)
                tracks, _ = client.get_artist_tracks(args.artist_id)
                albums, _ = client.get_artist_albums(args.artist_id)
                print(artist.label())
                print("\nSongs:")
                for track in tracks[:25]:
                    print(f"  [{track.id}] {track.label()}")
                print("\nAlbums:")
                for album in albums[:25]:
                    print(f"  [{album.id}] {album.label()}")
                return 0
            if command == "album":
                tracks, _ = client.get_album_tracks(args.album_id)
                if not tracks:
                    print("No tracks on this album.")
                    return 0
                for track in tracks:
                    print(f"[{track.id}] {track.label()}")
                return 0
            if command == "last":
                history = client.get_listening_history()
                if not history.tracks:
                    print(
                        "No online listening history yet. Play something in Melodify "
                        "(or here) so listens can accumulate."
                    )
                    return 0
                print(f"Listening history ({history.describe_source()}):")
                for track in history.tracks[: max(1, args.limit)]:
                    start = progress.resolve_start_seconds(
                        track.id,
                        api_progress=track.progress_seconds,
                        duration=track.duration,
                    )
                    suffix = ""
                    if start > 0:
                        mins, secs = divmod(int(start), 60)
                        suffix = f" · saved @{mins}:{secs:02d}"
                    print(f"  [{track.id}] {track.label()}{suffix}")
                print(
                    "\nTip: run `melodify continue` to resume the first track."
                )
                return 0
            if command == "continue":
                history = client.get_listening_history()
                if not history.tracks:
                    print(
                        "Nothing to continue — online listening history is empty.",
                        file=sys.stderr,
                    )
                    return 1
                queue = PlayQueue()
                track = queue.load(
                    history.tracks,
                    start=0,
                    source=history.describe_source(),
                )
                assert track is not None
                start = (
                    args.start_seconds
                    if args.start_seconds is not None
                    else progress.resolve_start_seconds(
                        track.id,
                        api_progress=track.progress_seconds,
                        duration=track.duration,
                    )
                )
                print(
                    f"Continue ({history.describe_source()}): {track.label()} "
                    f"[{queue.label()}]",
                    flush=True,
                )
                if start > 0:
                    mins, secs = divmod(int(start), 60)
                    print(f"Resuming at {mins}:{secs:02d}", flush=True)
                else:
                    print("No saved position — starting from the beginning.", flush=True)
                return _stream_queue(
                    client,
                    settings,
                    queue,
                    quality=args.quality,
                    start_seconds=start,
                    progress=progress,
                )
            if command == "play":
                results = client.search(args.query, search_type=1)
                if not results.tracks:
                    print(f'No tracks found for “{args.query}”.', file=sys.stderr)
                    return 1
                queue = PlayQueue()
                track = queue.load(results.tracks, start=0, source="search")
                assert track is not None
                start = (
                    args.start_seconds
                    if args.start_seconds is not None
                    else progress.resolve_start_seconds(
                        track.id,
                        api_progress=track.progress_seconds,
                        duration=track.duration,
                    )
                )
                print(f"Playing: {track.label()} [{queue.label()}]", flush=True)
                if start > 0:
                    mins, secs = divmod(int(start), 60)
                    print(f"Resuming at {mins}:{secs:02d}", flush=True)
                return _stream_queue(
                    client,
                    settings,
                    queue,
                    quality=args.quality,
                    start_seconds=start,
                    progress=progress,
                )
        except MelodifyAPIError as exc:
            print(f"API error: {exc}", file=sys.stderr)
            return 1
        except PlayerError as exc:
            print(f"Player error: {exc}", file=sys.stderr)
            return 3

    parser.print_help()
    return 2


def _stream_queue(
    client: MelodifyClient,
    settings,
    queue: PlayQueue,
    *,
    quality: str | None,
    start_seconds: float,
    progress: ProgressStore,
) -> int:
    print(f"Player:  {settings.player}", flush=True)
    player = StreamPlayer(preferred=settings.player)
    headers = {
        "authorization": settings.authorization,
        "user-id": settings.user_id,
        "device-id": settings.device_id,
        "device-token": settings.device_token,
        "Origin": settings.origin,
        "Referer": f"{settings.origin}/",
    }

    def _save_for_current(pos: float) -> None:
        current = queue.current
        if current is None:
            return
        progress.save_position(
            current.id, pos, title=current.title, mark_last=True
        )

    def _play_track(track: Track, *, seek: float = 0.0) -> None:
        url = client.build_play_url(track, quality=quality)
        client.record_listen(track)
        progress.save_position(
            track.id, seek, title=track.title, mark_last=True
        )
        player.play(
            url,
            title=track.label(),
            http_headers=headers,
            start_seconds=seek,
        )

    first = queue.current
    if first is None:
        print("Queue is empty.", file=sys.stderr)
        return 1
    _play_track(first, seek=start_seconds)
    interactive_playback_loop(
        player,
        on_position=_save_for_current,
        queue=queue,
        play_track=lambda track: _play_track(track, seek=0.0),
    )
    current = queue.current
    if current is not None and not player.is_alive:
        pos = progress.get_position(current.id)
        duration = current.duration or 0
        if pos is None or (duration and pos >= max(duration - 3, 1)):
            progress.clear_position(current.id)
    return 0


def _print_search(results) -> None:
    print(f"Songs ({len(results.tracks)}):")
    for track in results.tracks:
        print(f"  [{track.id}] {track.label()}")
    print(f"\nArtists ({len(results.artists)}):")
    for artist in results.artists:
        print(f"  [{artist.id}] {artist.label()}")
    print(f"\nAlbums ({len(results.albums)}):")
    for album in results.albums:
        print(f"  [{album.id}] {album.label()}")


if __name__ == "__main__":
    raise SystemExit(main())
