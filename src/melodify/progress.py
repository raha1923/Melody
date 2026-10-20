from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import playback_path


def default_state_path() -> Path:
    return playback_path()


@dataclass
class SavedProgress:
    track_id: int
    position_seconds: float
    updated_at: float
    title: str | None = None


class ProgressStore:
    """Local resume positions for tracks (API rarely exposes seek progress)."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"positions": {}, "last_track_id": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"positions": {}, "last_track_id": None}
        if not isinstance(data, dict):
            return {"positions": {}, "last_track_id": None}
        data.setdefault("positions", {})
        data.setdefault("last_track_id", None)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_position(self, track_id: int) -> float | None:
        data = self._load()
        raw = (data.get("positions") or {}).get(str(track_id))
        if not isinstance(raw, dict):
            return None
        try:
            position = float(raw.get("position_seconds") or 0)
        except (TypeError, ValueError):
            return None
        return position if position > 0 else None

    def save_position(
        self,
        track_id: int,
        position_seconds: float,
        *,
        title: str | None = None,
        mark_last: bool = True,
    ) -> None:
        data = self._load()
        positions = data.setdefault("positions", {})
        positions[str(track_id)] = {
            "position_seconds": max(0.0, float(position_seconds)),
            "updated_at": time.time(),
            "title": title,
        }
        if mark_last:
            data["last_track_id"] = track_id
        # Keep the map bounded.
        if len(positions) > 200:
            ordered = sorted(
                positions.items(),
                key=lambda item: float((item[1] or {}).get("updated_at") or 0),
            )
            for key, _ in ordered[: len(positions) - 200]:
                positions.pop(key, None)
        self._save(data)

    def clear_position(self, track_id: int) -> None:
        data = self._load()
        positions = data.get("positions") or {}
        if str(track_id) in positions:
            positions.pop(str(track_id), None)
            self._save(data)

    def resolve_start_seconds(
        self,
        track_id: int,
        *,
        api_progress: float | None = None,
        duration: int | None = None,
    ) -> float:
        """Prefer API progress when present; otherwise local saved position."""
        candidates = [api_progress, self.get_position(track_id)]
        for value in candidates:
            if value is None:
                continue
            start = float(value)
            if start <= 0:
                continue
            if duration is not None and duration > 0 and start >= max(duration - 3, 1):
                # Near the end — restart from the beginning.
                continue
            return start
        return 0.0
