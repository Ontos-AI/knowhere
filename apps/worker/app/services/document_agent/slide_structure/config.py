"""Settings for the slide-structure route."""

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
class SlideStructureConfig:
    sheet: GridLayout
    """Grid used to screen every slide and to look for gap sections."""
    candidates: GridLayout
    """Grid used to show the candidate structural slides together."""
    vlm_concurrency: int
    vlm_max_tokens: int

    @classmethod
    def from_settings(cls) -> "SlideStructureConfig":
        from shared.core.config import settings

        return cls(
            sheet=GridLayout(
                cols=settings.SLIDE_STRUCTURE_SHEET_COLS,
                rows=settings.SLIDE_STRUCTURE_SHEET_ROWS,
                thumb_width=settings.SLIDE_STRUCTURE_SHEET_THUMB_WIDTH,
            ),
            candidates=GridLayout(
                cols=settings.SLIDE_STRUCTURE_CANDIDATE_COLS,
                rows=settings.SLIDE_STRUCTURE_CANDIDATE_ROWS,
                thumb_width=settings.SLIDE_STRUCTURE_CANDIDATE_THUMB_WIDTH,
            ),
            vlm_concurrency=settings.SLIDE_STRUCTURE_VLM_CONCURRENCY,
            vlm_max_tokens=settings.SLIDE_STRUCTURE_VLM_MAX_TOKENS,
        )
