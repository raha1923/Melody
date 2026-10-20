"""Up next panel: compact window + signature stability (no tick blink)."""

from __future__ import annotations

from melodify.models import Track
from melodify.queue import PlayQueue
from melodify.tui import PlayerChrome


def _tracks(n: int) -> list[Track]:
    return [Track(id=i, title=f"T{i}") for i in range(1, n + 1)]


def test_compact_up_next_is_current_plus_few() -> None:
    chrome = PlayerChrome.__new__(PlayerChrome)
    chrome._expanded = False
    tracks = _tracks(20)
    q = PlayQueue()
    q.load(tracks, start=tracks[4], source="test")
    assert q.index == 4
    rows = chrome._up_next_window(q)
    assert len(rows) == 1 + PlayerChrome.COMPACT_UPCOMING
    assert rows[0][2] is True
    assert rows[0][1].id == 5
    assert [r[1].id for r in rows[1:]] == [6, 7, 8]


def test_expanded_shows_more_upcoming() -> None:
    chrome = PlayerChrome.__new__(PlayerChrome)
    chrome._expanded = True
    q = PlayQueue()
    q.load(_tracks(30), start=_tracks(30)[0], source="test")
    rows = chrome._up_next_window(q)
    assert len(rows) == 1 + PlayerChrome.EXPANDED_UPCOMING


def test_queue_signature_stable_across_progress_only_changes() -> None:
    """Same index/ids → same sig; progress ticks must not invalidate it."""
    chrome = PlayerChrome.__new__(PlayerChrome)
    chrome._expanded = False
    q = PlayQueue()
    q.load(_tracks(10), start=_tracks(10)[2], source="album")
    rows = chrome._up_next_window(q)
    sig = (
        q.index,
        len(q.tracks),
        q.source,
        chrome._expanded,
        tuple((i, t.id) for i, t, _ in rows),
    )
    # Simulate another tick with unchanged queue:
    rows2 = chrome._up_next_window(q)
    sig2 = (
        q.index,
        len(q.tracks),
        q.source,
        chrome._expanded,
        tuple((i, t.id) for i, t, _ in rows2),
    )
    assert sig == sig2
    q.advance()
    rows3 = chrome._up_next_window(q)
    sig3 = (
        q.index,
        len(q.tracks),
        q.source,
        chrome._expanded,
        tuple((i, t.id) for i, t, _ in rows3),
    )
    assert sig3 != sig
