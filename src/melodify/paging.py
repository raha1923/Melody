from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

# Match MelodifyClient.PAGE_FETCH_CAP — first-open auto-prefetch target.
PREFETCH_CAP = 500

# offset -> (page items, end flag from Melodify)
PageFetcher = Callable[[int], tuple[list[T], bool]]


@dataclass
class ListPager(Generic[T]):
    """Incremental Melodify offset pager. Appends pages; never drops loaded items."""

    fetch: PageFetcher[T]
    items: list[T] = field(default_factory=list)
    end: bool = False
    loading: bool = False
    prefetching: bool = False
    label: str = ""

    @property
    def has_more(self) -> bool:
        return (not self.end) and (not self.loading) and (not self.prefetching)

    @property
    def offset(self) -> int:
        return len(self.items)

    def replace(self, items: list[T], *, end: bool) -> None:
        self.items = list(items)
        self.end = bool(end)
        self.loading = False
        self.prefetching = False

    def load_more(self) -> list[T]:
        """Fetch the next page and append unique items. Returns newly added rows."""
        if self.end or self.loading:
            return []
        self.loading = True
        try:
            page, end = self.fetch(self.offset)
        except Exception:
            self.loading = False
            raise
        if not page:
            self.end = True
            self.loading = False
            return []
        seen: set[int] = set()
        for item in self.items:
            item_id = getattr(item, "id", None)
            if isinstance(item_id, int):
                seen.add(item_id)
        added: list[T] = []
        for item in page:
            item_id = getattr(item, "id", None)
            if isinstance(item_id, int):
                if item_id in seen:
                    continue
                seen.add(item_id)
            self.items.append(item)
            added.append(item)
        self.end = bool(end) or not added
        self.loading = False
        return added

    def prefetch_until(
        self,
        *,
        cap: int = PREFETCH_CAP,
        on_progress: Callable[[int, bool], None] | None = None,
    ) -> int:
        """Walk Melodify pages until ``end`` or ``cap`` items.

        If the API still has more after ``cap``, ``end`` stays False so Load more
        can continue. Returns the final item count.
        """
        if self.end or len(self.items) >= cap:
            return len(self.items)
        self.prefetching = True
        try:
            while not self.end and len(self.items) < cap:
                added = self.load_more()
                if len(self.items) > cap:
                    # Soft cap — keep end=False when the API still has pages.
                    api_said_end = self.end
                    self.items = self.items[:cap]
                    if not api_said_end:
                        self.end = False
                if on_progress is not None:
                    on_progress(len(self.items), self.end)
                if not added:
                    break
                if len(self.items) >= cap:
                    break
        finally:
            self.prefetching = False
        return len(self.items)
