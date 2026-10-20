from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

# offset -> (page items, end flag from Melodify)
PageFetcher = Callable[[int], tuple[list[T], bool]]


@dataclass
class ListPager(Generic[T]):
    """Incremental Melodify offset pager. Appends pages; never drops loaded items."""

    fetch: PageFetcher[T]
    items: list[T] = field(default_factory=list)
    end: bool = False
    loading: bool = False
    label: str = ""

    @property
    def has_more(self) -> bool:
        return (not self.end) and (not self.loading)

    @property
    def offset(self) -> int:
        return len(self.items)

    def replace(self, items: list[T], *, end: bool) -> None:
        self.items = list(items)
        self.end = bool(end)
        self.loading = False

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
