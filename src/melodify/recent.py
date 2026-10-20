from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import Artist, Mp3Link, Track


def default_recent_path() -> Path:
    return Path.home() / ".local" / "share" / "melodify" / "recent.json"


class RecentStore:
    """Local recently-played list (Melodify PWA keeps this offline too)."""

    def __init__(self, path: Path | None = None, *, cap: int = 50):
        self.path = path or default_recent_path()
        self.cap = max(1, cap)

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tracks": [], "updated_at": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"tracks": [], "updated_at": None}
        if not isinstance(data, dict):
            return {"tracks": [], "updated_at": None}
        data.setdefault("tracks", [])
        return data

    def _save_raw(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.time()
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_tracks(self) -> list[Track]:
        data = self._load_raw()
        tracks: list[Track] = []
        for raw in data.get("tracks") or []:
            if not isinstance(raw, dict):
                continue
            track = self._track_from_dict(raw)
            if track is not None:
                tracks.append(track)
        return tracks

    def upsert(self, track: Track, *, position_seconds: float | None = None) -> None:
        """Move track to the front (dedupe by id). Caps list length."""
        data = self._load_raw()
        entries = [
            raw
            for raw in (data.get("tracks") or [])
            if isinstance(raw, dict) and int(raw.get("id") or -1) != track.id
        ]
        payload = self._track_to_dict(track)
        payload["played_at"] = time.time()
        if position_seconds is not None:
            payload["position_seconds"] = max(0.0, float(position_seconds))
        entries.insert(0, payload)
        data["tracks"] = entries[: self.cap]
        self._save_raw(data)

    def is_empty(self) -> bool:
        return not self.list_tracks()

    @staticmethod
    def _track_to_dict(track: Track) -> dict[str, Any]:
        return {
            "id": track.id,
            "title": track.title,
            "duration": track.duration,
            "image": track.image,
            "downloadable": track.downloadable,
            "progress_seconds": track.progress_seconds,
            "artists": [
                {
                    "id": artist.id,
                    "name": artist.name,
                    "tracks_count": artist.tracks_count,
                    "play_count": artist.play_count,
                    "followers_count": artist.followers_count,
                    "image": artist.image,
                }
                for artist in track.artists
            ],
            "mp3s": [
                {"name": link.name, "quality": link.quality, "demo": link.demo}
                for link in track.mp3s
            ],
        }

    @staticmethod
    def _track_from_dict(raw: dict[str, Any]) -> Track | None:
        try:
            track_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            return None
        artists = [
            Artist(
                id=int(a["id"]),
                name=str(a.get("name") or "Unknown artist"),
                tracks_count=a.get("tracks_count"),
                play_count=a.get("play_count"),
                followers_count=a.get("followers_count"),
                image=a.get("image"),
            )
            for a in (raw.get("artists") or [])
            if isinstance(a, dict) and a.get("id") is not None
        ]
        mp3s = [
            Mp3Link(
                name=str(m.get("name") or ""),
                quality=str(m.get("quality") or ""),
                demo=int(m.get("demo") or 0),
            )
            for m in (raw.get("mp3s") or [])
            if isinstance(m, dict) and m.get("name")
        ]
        progress = raw.get("progress_seconds")
        if progress is None and raw.get("position_seconds") is not None:
            progress = raw.get("position_seconds")
        try:
            progress_f = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress_f = None
        return Track(
            id=track_id,
            title=str(raw.get("title") or "Untitled track"),
            duration=int(raw["duration"]) if raw.get("duration") is not None else None,
            artists=artists,
            mp3s=mp3s,
            image=raw.get("image"),
            downloadable=bool(raw.get("downloadable", True)),
            progress_seconds=progress_f if progress_f and progress_f > 0 else None,
        )
