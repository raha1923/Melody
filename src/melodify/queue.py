from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Track


@dataclass
class PlayQueue:
    """Simple in-memory play queue. No wrap at edges."""

    tracks: list[Track] = field(default_factory=list)
    index: int = 0
    source: str = ""
    # Optional Melodify ListPager[Track] so we can prefetch more pages near the end.
    pager: Any | None = None

    def clear(self) -> None:
        self.tracks = []
        self.index = 0
        self.source = ""
        self.pager = None

    @property
    def current(self) -> Track | None:
        if not self.tracks or self.index < 0 or self.index >= len(self.tracks):
            return None
        return self.tracks[self.index]

    @property
    def size(self) -> int:
        return len(self.tracks)

    def label(self) -> str:
        if not self.tracks:
            return "empty"
        return f"{self.index + 1}/{len(self.tracks)}" + (
            f" · {self.source}" if self.source else ""
        )

    def load(
        self,
        tracks: list[Track],
        *,
        start: Track | int | None = None,
        source: str = "",
    ) -> Track | None:
        """Replace the queue. Returns the track that should start playing."""
        cleaned = [t for t in tracks if isinstance(t, Track)]
        self.tracks = cleaned
        self.source = source
        self.pager = None
        if not cleaned:
            self.index = 0
            return None

        if isinstance(start, Track):
            for i, track in enumerate(cleaned):
                if track.id == start.id:
                    self.index = i
                    return track
            # Selected track not in list — prepend it.
            self.tracks = [start, *cleaned]
            self.index = 0
            return start

        if isinstance(start, int):
            self.index = max(0, min(start, len(cleaned) - 1))
        else:
            self.index = 0
        return self.current

    def remaining(self) -> int:
        if not self.tracks:
            return 0
        return max(0, len(self.tracks) - self.index - 1)

    def extend_unique(self, tracks: list[Track]) -> int:
        """Append tracks not already in the queue. Returns how many were added."""
        seen = {t.id for t in self.tracks}
        added = 0
        for track in tracks:
            if track.id in seen:
                continue
            self.tracks.append(track)
            seen.add(track.id)
            added += 1
        return added

    def peek_next(self) -> Track | None:
        if self.index + 1 < len(self.tracks):
            return self.tracks[self.index + 1]
        return None

    def peek_prev(self) -> Track | None:
        if self.index - 1 >= 0:
            return self.tracks[self.index - 1]
        return None

    def advance(self) -> Track | None:
        """Move to next track (no wrap). Returns None at end."""
        nxt = self.peek_next()
        if nxt is None:
            return None
        self.index += 1
        return nxt

    def retreat(self) -> Track | None:
        """Move to previous track (no wrap). Returns None at start."""
        prev = self.peek_prev()
        if prev is None:
            return None
        self.index -= 1
        return prev
