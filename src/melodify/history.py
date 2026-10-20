from __future__ import annotations

from .api import MelodifyClient
from .models import ListeningHistory
from .recent import RecentStore


def resolve_listening_history(
    client: MelodifyClient,
    recent: RecentStore,
    *,
    prefer_local: bool = True,
) -> ListeningHistory:
    """Prefer local recently-played; fall back to online most-listened."""
    if prefer_local:
        local = recent.list_tracks()
        if local:
            return ListeningHistory(
                tracks=local,
                source="local recently played",
                end=True,
            )
    online = client.get_listening_history()
    return online
