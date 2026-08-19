"""Debug-only TOC page policy for staged page_memory scripts.

Production page_memory no longer ships this helper; debug stages still need a
single place to read ``toc_result.toc_pages`` and filter processing ranges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TocPagePolicy:
    pure_toc_pages: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def from_anatomy(cls, anatomy: Any | None) -> TocPagePolicy:
        toc_result = getattr(anatomy, "toc_result", None) if anatomy is not None else None
        pages = getattr(toc_result, "toc_pages", None) or []
        pure: set[int] = set()
        for page in pages:
            try:
                pure.add(int(page))
            except (TypeError, ValueError):
                continue
        return cls(pure_toc_pages=frozenset(pure))

    def filter_processing_pages(self, pages: list[int]) -> list[int]:
        return [page for page in pages if page not in self.pure_toc_pages]

    def body_start_by_page(self) -> dict[int, float]:
        return {}
