from __future__ import annotations

import argparse
import sys

from . import __version__
from .api import MelodifyAPIError, MelodifyClient
from .config import load_settings
from .player import PlayerError, StreamPlayer, detect_player
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

    p_artist = sub.add_parser("artist", help="Show artist profile + top tracks.")
    p_artist.add_argument("artist_id", type=int)

    p_album = sub.add_parser("album", help="List album tracks.")
    p_album.add_argument("album_id", type=int)

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
            if command == "play":
                results = client.search(args.query, search_type=1)
                if not results.tracks:
                    print(f'No tracks found for “{args.query}”.', file=sys.stderr)
                    return 1
                track = results.tracks[0]
                url = client.build_play_url(track, quality=args.quality)
                print(f"Playing: {track.label()}")
                print(f"Player:  {settings.player}")
                player = StreamPlayer(preferred=settings.player)
                headers = {
                    "authorization": settings.authorization,
                    "user-id": settings.user_id,
                    "device-id": settings.device_id,
                    "device-token": settings.device_token,
                    "Origin": settings.origin,
                    "Referer": f"{settings.origin}/",
                }
                player.play(url, title=track.label(), http_headers=headers)
                try:
                    if player._proc is not None:
                        player._proc.wait()
                except KeyboardInterrupt:
                    player.stop()
                    print("\nStopped.")
                return 0
        except MelodifyAPIError as exc:
            print(f"API error: {exc}", file=sys.stderr)
            return 1
        except PlayerError as exc:
            print(f"Player error: {exc}", file=sys.stderr)
            return 3

    parser.print_help()
    return 2


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
