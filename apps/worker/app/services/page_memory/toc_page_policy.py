"""Unified TOC page policy for page_memory processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.document_agent.manifest import TocRegionBoundary


@dataclass(frozen=True)
class TocPagePolicy:
    """Processing policy derived from anatomy TOC regions.

    Pure TOC pages are excluded from fine hierarchy, render/tag/assets, and
    body node ownership. Mixed boundary pages remain in the body pipeline and
    may carry a verbatim ``body_start_text`` anchor.
    """

    pure_toc_pages: frozenset[int]
    mixed_boundary_by_page: dict[int, str]
    regions: tuple[TocRegionBoundary, ...]

    @classmethod
    def from_anatomy(cls, anatomy: Any | None) -> TocPagePolicy:
        toc_result = getattr(anatomy, "toc_result", None)
        if toc_result is None:
            return cls(frozenset(), {}, ())

        raw_regions = list(getattr(toc_result, "regions", None) or [])
        regions: list[TocRegionBoundary] = []
        for item in raw_regions:
            if isinstance(item, TocRegionBoundary):
                regions.append(item)
                continue
            if isinstance(item, dict):
                regions.append(
                    TocRegionBoundary(
                        toc_pages=[int(p) for p in item.get("toc_pages") or []],
                        pure_toc_pages=[
                            int(p) for p in item.get("pure_toc_pages") or []
                        ],
                        mixed_page=(
                            int(item["mixed_page"])
                            if item.get("mixed_page") is not None
                            else None
                        ),
                        body_start_text=str(item.get("body_start_text") or ""),
                        reason=str(item.get("reason") or ""),
                    )
                )

        if regions:
            pure: set[int] = set()
            mixed: dict[int, str] = {}
            for region in regions:
                pure.update(int(page) for page in region.pure_toc_pages)
                if region.mixed_page is None:
                    continue
                # Mixed page stays in body processing even without an anchor.
                pure.discard(int(region.mixed_page))
                text = (region.body_start_text or "").strip()
                if text:
                    mixed[int(region.mixed_page)] = text
            return cls(frozenset(pure), mixed, tuple(regions))

        # Older anatomy without region probing: exclude all TOC pages.
        toc_pages = {
            int(page) for page in (getattr(toc_result, "toc_pages", None) or [])
        }
        return cls(frozenset(toc_pages), {}, ())

    def filter_processing_pages(self, pages: list[int]) -> list[int]:
        return [page for page in pages if page not in self.pure_toc_pages]

    def body_start_text(self, page: int) -> str:
        return self.mixed_boundary_by_page.get(int(page), "")

    def body_start_by_page(self) -> dict[int, str]:
        return dict(self.mixed_boundary_by_page)
