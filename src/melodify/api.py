from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .models import (
    Album,
    Artist,
    ListeningHistory,
    SearchResults,
    Track,
    parse_search_result,
)


class MelodifyAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class MelodifyClient:
    """Thin HTTP client for Melodify desktop API + CDN play URLs."""

    def __init__(self, settings: Settings, *, timeout: float = 45.0):
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.api_base,
            timeout=timeout,
            headers=self._auth_headers(),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MelodifyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _auth_headers(self) -> dict[str, str]:
        s = self.settings
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "authorization": s.authorization,
            "user-id": s.user_id,
            "device-id": s.device_id,
            "device-token": s.device_token,
            "device-name": s.device_name,
            "platform": s.platform,
            "app-version": s.app_version,
            "pwa-version": s.pwa_version,
            "Origin": s.origin,
            "Referer": f"{s.origin}/",
            "user-device-info": "screen_w:1280-screen_h:720-terminal:1",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self._client.request(method, path, json=json, params=params)
            except httpx.TimeoutException as exc:
                last_error = MelodifyAPIError(
                    "Request timed out. Melodify’s API is occasionally slow — try again."
                )
                if attempt < retries:
                    time.sleep(0.6 * attempt)
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                raise MelodifyAPIError(f"Network error talking to Melodify: {exc}") from exc

            if response.status_code >= 500:
                last_error = MelodifyAPIError(
                    f"Melodify server error ({response.status_code}). Try again shortly.",
                    status_code=response.status_code,
                )
                if attempt < retries:
                    time.sleep(0.6 * attempt)
                    continue
                raise last_error

            if response.status_code in (401, 403):
                raise MelodifyAPIError(
                    "Auth rejected. Check MELODIFY_AUTHORIZATION / device headers in .env.",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise MelodifyAPIError(
                    f"API request failed ({response.status_code}): {response.text[:200]}",
                    status_code=response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise MelodifyAPIError("API returned a non-JSON response.") from exc

            if isinstance(payload, dict) and payload.get("done") is False:
                raise MelodifyAPIError(
                    str(
                        payload.get("message")
                        or payload.get("error")
                        or "API returned done=false"
                    )
                )
            if not isinstance(payload, dict):
                raise MelodifyAPIError("Unexpected API payload shape.")
            return payload

        if last_error:
            raise last_error
        raise MelodifyAPIError("Request failed after retries.")

    # Melodify returns ~40 items per page; keep a hard cap so a huge catalog
    # cannot hang the TUI forever.
    PAGE_FETCH_CAP = 500

    def search(self, query: str, *, search_type: int = 6, offset: int = 0) -> SearchResults:
        payload = self._request(
            "POST",
            "/search",
            json={"q": query, "type": search_type, "offset": offset},
        )
        return parse_search_result(payload)

    def search_all(
        self,
        query: str,
        *,
        search_type: int = 6,
        max_items: int | None = None,
    ) -> SearchResults:
        """Paginate search until ``end`` (or ``max_items``). Mixed-type search
        merges pages for tracks/artists/albums independently by list length.
        """
        cap = max_items if max_items is not None else self.PAGE_FETCH_CAP
        first = self.search(query, search_type=search_type, offset=0)
        if first.end or search_type == 6:
            # Type 6 (all) pages are awkward to continue by a single offset;
            # one page is enough for the home search UX.
            return first
        tracks = list(first.tracks)
        artists = list(first.artists)
        albums = list(first.albums)
        offset = len(tracks) or len(artists) or len(albums)
        end = first.end
        while not end and offset < cap:
            page = self.search(query, search_type=search_type, offset=offset)
            if search_type == 1:
                if not page.tracks:
                    break
                tracks.extend(page.tracks)
                offset = len(tracks)
            elif search_type == 2:
                if not page.artists:
                    break
                artists.extend(page.artists)
                offset = len(artists)
            elif search_type == 3:
                if not page.albums:
                    break
                albums.extend(page.albums)
                offset = len(albums)
            else:
                break
            end = page.end
            if len(page.tracks) + len(page.artists) + len(page.albums) == 0:
                break
        return SearchResults(
            artists=artists[:cap],
            albums=albums[:cap],
            tracks=tracks[:cap],
            end=end or (len(tracks) + len(artists) + len(albums) >= cap),
        )

    def get_artist(self, artist_id: int) -> Artist:
        payload = self._request(
            "GET",
            "/getArtistData",
            params={"artist_id": artist_id},
        )
        artist = Artist.from_dict((payload.get("result") or {}).get("artist"))
        if not artist:
            raise MelodifyAPIError(f"Artist {artist_id} not found.")
        return artist

    def get_artist_tracks(
        self,
        artist_id: int,
        *,
        offset: int = 0,
        sort_by: str = "popular",
    ) -> tuple[list[Track], bool]:
        payload = self._request(
            "POST",
            "/getArtistTracks",
            json={"artist_id": artist_id, "offset": offset, "sort_by": sort_by},
        )
        result = payload.get("result") or {}
        tracks = [
            t
            for t in (Track.from_dict(raw) for raw in result.get("tracks") or [])
            if t is not None
        ]
        return tracks, bool(result.get("end", True))

    def get_artist_tracks_all(
        self,
        artist_id: int,
        *,
        sort_by: str = "popular",
        max_items: int | None = None,
    ) -> list[Track]:
        return self._paginate_tracks(
            lambda offset: self.get_artist_tracks(
                artist_id, offset=offset, sort_by=sort_by
            ),
            max_items=max_items,
        )

    def get_artist_albums(
        self, artist_id: int, *, offset: int = 0
    ) -> tuple[list[Album], bool]:
        payload = self._request(
            "POST",
            "/getArtistAlbums",
            json={"artist_id": artist_id, "offset": offset},
        )
        result = payload.get("result") or {}
        albums = [
            a
            for a in (Album.from_dict(raw) for raw in result.get("albums") or [])
            if a is not None
        ]
        return albums, bool(result.get("end", True))

    def get_artist_albums_all(
        self, artist_id: int, *, max_items: int | None = None
    ) -> list[Album]:
        cap = max_items if max_items is not None else self.PAGE_FETCH_CAP
        all_albums: list[Album] = []
        offset = 0
        seen: set[int] = set()
        while offset < cap:
            page, end = self.get_artist_albums(artist_id, offset=offset)
            if not page:
                break
            for album in page:
                if album.id in seen:
                    continue
                seen.add(album.id)
                all_albums.append(album)
                if len(all_albums) >= cap:
                    return all_albums
            offset += len(page)
            if end:
                break
        return all_albums

    def get_album_tracks(
        self, album_id: int, *, offset: int = 0
    ) -> tuple[list[Track], bool]:
        payload = self._request(
            "POST",
            "/getAlbumTracks",
            json={"album_id": album_id, "offset": offset},
        )
        result = payload.get("result") or {}
        tracks = [
            t
            for t in (Track.from_dict(raw) for raw in result.get("tracks") or [])
            if t is not None
        ]
        return tracks, bool(result.get("end", True))

    def get_album_tracks_all(
        self, album_id: int, *, max_items: int | None = None
    ) -> list[Track]:
        return self._paginate_tracks(
            lambda offset: self.get_album_tracks(album_id, offset=offset),
            max_items=max_items,
        )

    def get_most_listened_tracks(
        self, *, offset: int = 0
    ) -> tuple[list[Track], bool]:
        payload = self._request(
            "POST",
            "/getUserMostListenedTracks",
            json={"offset": offset},
        )
        result = payload.get("result") or {}
        tracks = [
            t
            for t in (Track.from_dict(raw) for raw in result.get("tracks") or [])
            if t is not None
        ]
        return tracks, bool(result.get("end", True))

    def get_most_listened_tracks_all(
        self, *, max_items: int | None = None
    ) -> list[Track]:
        return self._paginate_tracks(
            lambda offset: self.get_most_listened_tracks(offset=offset),
            max_items=max_items,
        )

    def _paginate_tracks(
        self,
        fetch_page,
        *,
        max_items: int | None = None,
    ) -> list[Track]:
        """Walk Melodify ``offset`` pages until ``end`` or empty/cap."""
        cap = max_items if max_items is not None else self.PAGE_FETCH_CAP
        all_tracks: list[Track] = []
        offset = 0
        seen: set[int] = set()
        while offset < cap:
            page, end = fetch_page(offset)
            if not page:
                break
            for track in page:
                if track.id in seen:
                    continue
                seen.add(track.id)
                all_tracks.append(track)
                if len(all_tracks) >= cap:
                    return all_tracks
            offset += len(page)
            if end:
                break
        return all_tracks

    def get_playlist_tracks_by_type(
        self, playlist_type: str, *, offset: int = 0
    ) -> tuple[list[Track], bool]:
        payload = self._request(
            "POST",
            "/getPlaylistTracksByType",
            json={"offset": offset, "type": playlist_type},
        )
        result = payload.get("result")
        if not result:
            return [], True
        tracks = [
            t
            for t in (Track.from_dict(raw) for raw in result.get("tracks") or [])
            if t is not None
        ]
        return tracks, bool(result.get("end", True))

    def get_listening_history(self, *, offset: int = 0) -> ListeningHistory:
        """Fetch online listening history for continue / last-played.

        The desktop PWA’s “اخیرا شنیده های من” UI is local Redux. Online, Melodify
        aggregates listens via ``/getUserMostListenedTracks``. We briefly probe
        ``getPlaylistTracksByType`` ``recently_listened`` (often ``result: null``
        and sometimes slow) before falling back to most-listened.
        """
        try:
            recent = self._probe_recently_listened(offset=offset)
            if recent:
                return ListeningHistory(
                    tracks=recent, source="recently_listened", end=True
                )
        except MelodifyAPIError:
            pass

        tracks, end = self.get_most_listened_tracks(offset=offset)
        return ListeningHistory(tracks=tracks, source="most_listened", end=end)

    def _probe_recently_listened(self, *, offset: int = 0) -> list[Track]:
        """Short-timeout probe — must not block continue/last on a hung endpoint."""
        try:
            response = self._client.post(
                "/getPlaylistTracksByType",
                json={"offset": offset, "type": "recently_listened"},
                timeout=8.0,
            )
        except httpx.TimeoutException as exc:
            raise MelodifyAPIError("recently_listened probe timed out") from exc
        except httpx.HTTPError as exc:
            raise MelodifyAPIError(f"recently_listened probe failed: {exc}") from exc
        if response.status_code >= 400:
            raise MelodifyAPIError(
                f"recently_listened probe HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MelodifyAPIError("recently_listened returned non-JSON") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        if not result:
            return []
        return [
            t
            for t in (Track.from_dict(raw) for raw in result.get("tracks") or [])
            if t is not None
        ]

    def record_listen(self, track: Track, *, is_demo: bool = False) -> None:
        """Notify Melodify that a track was listened to (same as PWA ``/listens``)."""
        try:
            self._request(
                "POST",
                "/listens",
                json={"track_id": track.id, "is_demo": is_demo},
                retries=2,
            )
        except MelodifyAPIError:
            # Non-fatal: playback should still work if listen accounting fails.
            return

    def build_play_url(self, track: Track, *, quality: str | None = None) -> str:
        quality = quality or self.settings.stream_quality
        path = track.stream_path(quality)
        if not path:
            raise MelodifyAPIError("This track has no streamable MP3 links.")

        # API already returns absolute CDN handle URLs in mp3s[].name
        query = urlencode(
            {
                "type": "pwa",
                "melodify_token": self.settings.user_id,
                "download_token": self.settings.authorization,
            }
        )
        # PWA appends stamp without '=' — keep that quirk.
        stamp = f"stamp{int(time.time() * 1000)}"
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}{query}&{stamp}"

    def resolve_stream_url(self, track: Track, *, quality: str | None = None) -> str:
        """Resolve the 307 play handle to a final media URL without buffering audio."""
        play_url = self.build_play_url(track, quality=quality)
        headers = {
            "authorization": self.settings.authorization,
            "user-id": self.settings.user_id,
            "device-id": self.settings.device_id,
            "device-token": self.settings.device_token,
            "device-name": self.settings.device_name,
            "platform": self.settings.platform,
            "app-version": self.settings.app_version,
            "pwa-version": self.settings.pwa_version,
            "Origin": self.settings.origin,
            "Referer": f"{self.settings.origin}/",
            "Range": "bytes=0-0",
        }
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                response = client.head(play_url, headers=headers)
                if response.status_code >= 400:
                    # Some CDN nodes dislike HEAD; fall back to a tiny ranged GET.
                    response = client.get(play_url, headers=headers)
                if response.status_code < 400:
                    return str(response.url)
        except httpx.HTTPError:
            pass
        # mpv/ffplay can follow the handle redirect themselves.
        return play_url
