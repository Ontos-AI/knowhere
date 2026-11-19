"""Settings for the slide-structure route."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.document_agent.page_screen.config import GridLayout, PageScreenConfig


@dataclass(frozen=True)
class SlideStructureConfig:
    sheet: GridLayout
    """Grid used to look for gap sections; the page screen's grid."""
    candidates: GridLayout
    """Grid used to show the candidate structural slides together."""
    vlm_concurrency: int
    vlm_max_tokens: int

    @classmethod
    def from_settings(cls) -> "SlideStructureConfig":
        from shared.core.config import settings

        return cls(
            sheet=PageScreenConfig.from_settings().sheet,
            candidates=GridLayout(
                cols=settings.SLIDE_STRUCTURE_CANDIDATE_COLS,
                rows=settings.SLIDE_STRUCTURE_CANDIDATE_ROWS,
                thumb_width=settings.SLIDE_STRUCTURE_CANDIDATE_THUMB_WIDTH,
            ),
            vlm_concurrency=settings.SLIDE_STRUCTURE_VLM_CONCURRENCY,
            vlm_max_tokens=settings.SLIDE_STRUCTURE_VLM_MAX_TOKENS,
        )
