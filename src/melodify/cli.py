from __future__ import annotations

import argparse
import sys

from . import __version__
from .api import InactiveSessionError, MelodifyAPIError, MelodifyClient
from .auth import AuthError, interactive_login
from .config import load_settings
from .models import DeviceSessions, Track
from .controls import interactive_playback_loop
from .history import resolve_listening_history
from .mpris_bridge import build_direct_actions
from .os_media import create_media_bridge
from .paths import data_dir
from .player import PlayerError, StreamPlayer, detect_player
from .progress import ProgressStore
from .queue import PlayQueue
from .recent import RecentStore
from .session import clear_session, load_session, session_path
from .tui import run_tui


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="melodify",
        description="Ubuntu/macOS terminal music client for Melodify (search, browse, stream).",
    )
    parser.add_argument("--version", action="version", version=f"melodify {__version__}")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional legacy .env file (default: ./.env). Prefer `melodify login`.",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Launch the interactive TUI (default).")

    p_login = sub.add_parser(
        "login",
        help="Log in with phone + SMS code (saves session under the OS data dir).",
    )
    p_login.add_argument(
        "--phone",
        default=None,
        help="Iranian mobile (09xxxxxxxxx). Prompted if omitted.",
    )
    p_login.add_argument(
        "--name",
        default=None,
        help="Display name for first-time registration only.",
    )
    sub.add_parser("logout", help="Clear the saved Melodify session.")
    sub.add_parser("whoami", help="Show the active session (env or session file).")

    p_devices = sub.add_parser(
        "devices",
        help="List listening sessions, or make this install the one that can play.",
    )
    p_devices.add_argument(
        "--remove",
        metavar="DEVICE_TOKEN",
        default=None,
        help="Remove one other session by its device token.",
    )
    p_devices.add_argument(
        "--use-this",
        action="store_true",
        help="Remove every other session so this install can play music.",
    )
    p_devices.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt for --use-this.",
    )

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
        help="List recently played (local first; online most-listened fallback).",
    )
    p_last.add_argument(
        "--limit",
        type=int,
        default=25,
        help="How many tracks to show (default: 25)",
    )

    p_continue = sub.add_parser(
        "continue",
        help="Resume the top recently-played track (local first; online fallback).",
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

    sub.add_parser(
        "doctor",
        help="Check OS media integration (MPRIS / D-Bus / mpv) without needing .env.",
    )

    args = parser.parse_args(argv)
    command = args.command or "tui"

    if command == "doctor":
        return _run_doctor()

    if command == "login":
        try:
            session = interactive_login(phone=args.phone, name=args.name)
        except (AuthError, EOFError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                print("\nCancelled.", file=sys.stderr)
                return 130
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1
        print(f"Logged in as {session.full_name or session.user_id}")
        print(f"Session saved to {session_path()} (mode 0600)")
        print("Run `melodify` or `melodify tui` to start.")
        return 0

    if command == "logout":
        if clear_session():
            print(f"Cleared session at {session_path()}")
        else:
            print("No session file to clear.")
        print(
            "Note: MELODIFY_* env vars in .env still override if set — unset them "
            "or remove .env to fully sign out."
        )
        return 0

    if command == "whoami":
        return _run_whoami(args.env_file)

    settings = load_settings(args.env_file)
    missing = settings.missing()
    if missing and command == "tui":
        # TUI shows an in-app login screen when unauthenticated.
        try:
            detect_player(settings.player)
        except PlayerError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        run_tui(settings)
        return 0
    if missing:
        print(
            "Not logged in (missing: "
            + ", ".join(missing)
            + ").\n\n"
            "  melodify login\n\n"
            "Legacy: copy .env.example → .env with session tokens (env overrides session).",
            file=sys.stderr,
        )
        return 2
    progress = ProgressStore()
    recent = RecentStore()

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
            if command == "devices":
                return _run_devices(client, args)
            if command == "search":
                results = client.search(args.query, search_type=args.search_type)
                _print_search(results)
                return 0
            if command == "artist":
                artist = client.get_artist(args.artist_id)
                tracks = client.get_artist_tracks_all(args.artist_id)
                albums = client.get_artist_albums_all(args.artist_id)
                print(artist.label())
                print(f"\nSongs ({len(tracks)}):")
                for track in tracks:
                    print(f"  [{track.id}] {track.label()}")
                print(f"\nAlbums ({len(albums)}):")
                for album in albums:
                    print(f"  [{album.id}] {album.label()}")
                return 0
            if command == "album":
                tracks = client.get_album_tracks_all(args.album_id)
                if not tracks:
                    print("No tracks on this album.")
                    return 0
                print(f"{len(tracks)} tracks:")
                for track in tracks:
                    print(f"[{track.id}] {track.label()}")
                return 0
            if command == "last":
                history = resolve_listening_history(client, recent)
                if not history.tracks:
                    print(
                        "No local recently-played tracks yet, and online history is empty.\n"
                        "Play something with `melodify play` / the TUI first."
                    )
                    return 0
                print(f"Listening history ({history.describe_source()}):")
                print(f"(file: {recent.path})")
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
                history = resolve_listening_history(client, recent)
                if not history.tracks:
                    print(
                        "Nothing to continue — local and online history are empty.",
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
                    recent=recent,
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
                    recent=recent,
                )
        except InactiveSessionError as exc:
            _print_inactive_session(exc)
            return 1
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
    recent: RecentStore,
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
        recent.upsert(current, position_seconds=pos)

    media = create_media_bridge()

    def _play_track(track: Track, *, seek: float = 0.0) -> None:
        url = client.build_play_url(track, quality=quality)
        progress.save_position(
            track.id, seek, title=track.title, mark_last=True
        )
        recent.upsert(track, position_seconds=seek)
        player.play(
            url,
            title=track.label(),
            http_headers=headers,
            start_seconds=seek,
        )
        try:
            client.record_listen(track)
        except Exception:  # noqa: BLE001
            pass
        media.notify(title=True, playpause=True)

    media.attach(
        player=player,
        queue=queue,
        get_track=lambda: queue.current,
        actions=build_direct_actions(
            player,
            queue,
            play_track=lambda track: _play_track(track, seek=0.0),
        ),
    )
    if media.start():
        print("OS media controls enabled.", flush=True)
    elif getattr(media, "last_error", None):
        print(f"OS media controls off: {media.last_error}", flush=True)

    first = queue.current
    if first is None:
        print("Queue is empty.", file=sys.stderr)
        media.stop()
        return 1
    try:
        _play_track(first, seek=start_seconds)
        interactive_playback_loop(
            player,
            on_position=_save_for_current,
            queue=queue,
            play_track=lambda track: _play_track(track, seek=0.0),
            on_tick=media.sync_from_player,
        )
    finally:
        media.stop()
    current = queue.current
    if current is not None and not player.is_alive:
        pos = progress.get_position(current.id)
        duration = current.duration or 0
        if pos is None or (duration and pos >= max(duration - 3, 1)):
            progress.clear_position(current.id)
    return 0


def _print_inactive_session(exc: InactiveSessionError) -> None:
    print(str(exc), file=sys.stderr)
    labels = " · ".join(part for part in (exc.btn_1_text, exc.btn_2_text) if part)
    if labels:
        print(labels, file=sys.stderr)
    print(
        "Another device is the active listening session.\n"
        "  melodify devices\n"
        "  melodify devices --use-this",
        file=sys.stderr,
    )


def _print_sessions(sessions: DeviceSessions) -> None:
    current = sessions.current
    print(f"This device: {current.name if current else 'This device'}")
    if not sessions.others:
        print("Other devices: none")
        return
    print(f"Other devices ({len(sessions.others)}):")
    for item in sessions.others:
        activity = f" · last seen {item.last_activity}" if item.last_activity else ""
        print(f"  {item.name}{activity}")
        token = item.device_token or "(no token)"
        print(f"    device_token: {token}")


def _confirm_use_this(*, assume_yes: bool) -> bool | None:
    """True to proceed, False if declined, None if a terminal confirmation is required."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Refusing to remove other devices without --yes when stdin is not a terminal.",
            file=sys.stderr,
        )
        return None
    try:
        answer = input(
            "Remove all other Melodify devices so this one can play? [y/N] "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _run_devices(client: MelodifyClient, args: argparse.Namespace) -> int:
    if args.remove and args.use_this:
        print("Use either --remove or --use-this, not both.", file=sys.stderr)
        return 2
    if args.use_this:
        confirmed = _confirm_use_this(assume_yes=args.yes)
        if confirmed is None:
            return 2
        if not confirmed:
            print("Cancelled.")
            return 0
        client.remove_other_sessions()
        print("This install is now the listening device.")
    elif args.remove:
        client.remove_session(args.remove)
        print("Removed that session.")
    sessions = client.list_sessions()
    _print_sessions(sessions)
    if sessions.others and not args.use_this:
        print("\nTo listen here: melodify devices --use-this")
        print("To drop one other device: melodify devices --remove <device_token>")
    return 0


def _run_whoami(env_file: str | None) -> int:
    settings = load_settings(env_file)
    session = load_session()
    print(f"Auth source: {settings.auth_source}")
    print(f"Data dir:    {data_dir()}")
    print(f"Session:     {session_path()}" + (" (present)" if session else " (missing)"))
    if not settings.authenticated:
        print("Status:      not logged in — run `melodify login`")
        return 1
    name = ""
    phone = ""
    if session:
        name = session.full_name
        phone = session.phone
    print(f"User id:     {settings.user_id}")
    if name:
        print(f"Name:        {name}")
    if phone:
        # Mask middle digits
        masked = phone[:4] + "****" + phone[-3:] if len(phone) >= 8 else phone
        print(f"Phone:       {masked}")
    print(f"Device id:   {settings.device_id[:12]}…")
    print(f"Device name: {settings.device_name}")
    print(f"Platform:    {settings.platform}")
    return 0


def _run_doctor() -> int:
    from .os_media import diagnose_os_media
    from .paths import is_macos

    print(f"Melodify {__version__} — OS media / environment check\n")
    rows = diagnose_os_media()
    width = max(len(name) for name, _, _ in rows)
    failed = 0
    critical = {
        "mpv",
        "MELODIFY_MPRIS",
        "mpris-server",
        "PyGObject (gi)",
        "D-Bus session",
        "MPRIS publish",
        "MELODIFY_NOWPLAYING",
        "MediaPlayer (PyObjC)",
        "Now Playing",
    }
    for name, ok, detail in rows:
        mark = "OK " if ok else "NO "
        print(f"  [{mark}] {name:<{width}}  {detail}")
        if not ok and name in critical and name not in {"Homebrew", "playerctl"}:
            failed += 1
    if is_macos():
        print(
            "\nOn macOS, with Melodify playing:\n"
            "  Check Control Center → Now Playing, or press media keys\n"
            "\nInstall tips:\n"
            "  brew install mpv\n"
            "  python3 -m venv .venv && source .venv/bin/activate\n"
            "  pip install -e '.[macos]'\n"
        )
    else:
        print(
            "\nOn your Ubuntu desktop, with Melodify playing:\n"
            "  playerctl -p Melodify status\n"
            "  playerctl -p Melodify play-pause\n"
            "  playerctl -p Melodify next\n"
            "\nIf PyGObject/mpris failed, use apt + --no-deps (do not pip-build cairo):\n"
            "  sudo apt install -y python3-gi gir1.2-glib-2.0 playerctl\n"
            "  python3 -m venv --system-site-packages .venv && source .venv/bin/activate\n"
            "  pip install -e '.[mpris]'\n"
            "  pip install --no-deps mpris-server\n"
        )
    return 1 if failed else 0


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
