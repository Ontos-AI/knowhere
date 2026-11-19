"""Settings for the page-image screen."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GridLayout:
    cols: int
    rows: int
    thumb_width: int

    @property
    def cells(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True)
class PageScreenConfig:
    sheet: GridLayout
    """Grid used to screen every page."""
    vlm_concurrency: int
    vlm_max_tokens: int

    @classmethod
    def from_settings(cls) -> "PageScreenConfig":
        from shared.core.config import settings

        return cls(
            sheet=GridLayout(
                cols=settings.PAGE_SCREEN_SHEET_COLS,
                rows=settings.PAGE_SCREEN_SHEET_ROWS,
                thumb_width=settings.PAGE_SCREEN_SHEET_THUMB_WIDTH,
            ),
            vlm_concurrency=settings.PAGE_SCREEN_VLM_CONCURRENCY,
            vlm_max_tokens=settings.PAGE_SCREEN_VLM_MAX_TOKENS,
        )
