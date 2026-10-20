from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Mp3Link:
    name: str
    quality: str
    demo: int = 0


@dataclass
class Artist:
    id: int
    name: str
    tracks_count: int | None = None
    play_count: str | None = None
    followers_count: str | None = None
    image: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Artist | None:
        if not data:
            return None
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or "Unknown artist"),
            tracks_count=_maybe_int(data.get("tracks_count")),
            play_count=_maybe_str(data.get("play_count")),
            followers_count=_maybe_str(data.get("followers_count")),
            image=_maybe_str(data.get("image")),
        )

    def label(self) -> str:
        bits = [self.name]
        if self.tracks_count is not None:
            bits.append(f"{self.tracks_count} tracks")
        if self.play_count:
            bits.append(f"{self.play_count} plays")
        return " · ".join(bits)


@dataclass
class Album:
    id: int
    title: str
    tracks_count: int | None = None
    artists: list[Artist] = field(default_factory=list)
    image: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Album | None:
        if not data:
            return None
        artists = [
            a
            for a in (Artist.from_dict(raw) for raw in data.get("artists") or [])
            if a is not None
        ]
        return cls(
            id=int(data["id"]),
            title=str(data.get("title") or "Untitled album"),
            tracks_count=_maybe_int(data.get("tracks_count")),
            artists=artists,
            image=_maybe_str(data.get("image")),
        )

    def artist_names(self) -> str:
        if self.artists:
            return ", ".join(a.name for a in self.artists)
        return "Various artists"

    def label(self) -> str:
        bits = [self.title, self.artist_names()]
        if self.tracks_count is not None:
            bits.append(f"{self.tracks_count} tracks")
        return " · ".join(bits)


@dataclass
class Track:
    id: int
    title: str
    duration: int | None = None
    artists: list[Artist] = field(default_factory=list)
    mp3s: list[Mp3Link] = field(default_factory=list)
    image: str | None = None
    downloadable: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Track | None:
        if not data:
            return None
        artists = [
            a
            for a in (Artist.from_dict(raw) for raw in data.get("artists") or [])
            if a is not None
        ]
        mp3s = [
            Mp3Link(
                name=str(raw.get("name") or ""),
                quality=str(raw.get("quality") or ""),
                demo=int(raw.get("demo") or 0),
            )
            for raw in data.get("mp3s") or []
            if raw.get("name")
        ]
        return cls(
            id=int(data["id"]),
            title=str(data.get("title") or "Untitled track"),
            duration=_maybe_int(data.get("duration")),
            artists=artists,
            mp3s=mp3s,
            image=_maybe_str(data.get("image")),
            downloadable=bool(data.get("downloadable", True)),
        )

    def artist_names(self) -> str:
        if self.artists:
            return ", ".join(a.name for a in self.artists)
        return "Unknown artist"

    def duration_label(self) -> str:
        if self.duration is None:
            return "--:--"
        minutes, seconds = divmod(max(0, self.duration), 60)
        return f"{minutes}:{seconds:02d}"

    def label(self) -> str:
        return f"{self.title} · {self.artist_names()} · {self.duration_label()}"

    def stream_path(self, quality: str = "320") -> str | None:
        preferred = next((m for m in self.mp3s if m.quality == quality), None)
        chosen = preferred or (self.mp3s[0] if self.mp3s else None)
        return chosen.name if chosen else None


@dataclass
class SearchResults:
    artists: list[Artist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    end: bool = True


def _unwrap_model(item: dict[str, Any], key: str) -> dict[str, Any] | None:
    if key in item and isinstance(item[key], dict):
        return item[key]
    if "id" in item:
        return item
    return None


def parse_search_result(payload: dict[str, Any]) -> SearchResults:
    result = payload.get("result") or {}
    artists = [
        a
        for a in (
            Artist.from_dict(_unwrap_model(item, "artist"))
            for item in result.get("artists") or []
        )
        if a is not None
    ]
    albums = [
        a
        for a in (
            Album.from_dict(_unwrap_model(item, "album"))
            for item in result.get("albums") or []
        )
        if a is not None
    ]
    tracks = [
        t
        for t in (
            Track.from_dict(_unwrap_model(item, "track"))
            for item in result.get("tracks") or []
        )
        if t is not None
    ]
    return SearchResults(
        artists=artists,
        albums=albums,
        tracks=tracks,
        end=bool(result.get("end", True)),
    )


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
