"""PageMap — the output contract of the Document Anatomy Agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


# ── Page-level feature snapshot ───────────────────────────────────────────────


@dataclass
class PageFeature:
    """Raw structural measurements for a single PDF page (1-based numbering)."""

    page: int
    text_length: int
    text_density: float          # chars per 10k pt² of page area
    image_coverage: float        # fraction [0, 1]
    image_count: int
    table_count: int
    drawings_count: int
    orientation: Literal["portrait", "landscape"]
    width: float                 # points
    height: float                # points
    is_blank_like: bool
    text_preview: str            # first N chars of page text (configurable)

    # Reserved for future Page Memory integration
    embedding_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── H1 boundary detection result ─────────────────────────────────────────────


@dataclass
class H1Match:
    """A single level-1 heading found via text search."""

    title: str           # normalized heading text from TOC
    page: int            # 1-based page where heading was found in the body
    confidence: float    # 1.0 = exact match, < 1.0 = fuzzy
    match_text: str      # the actual text snippet that matched on the page

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class H1BoundaryResult:
    """Output of the ``find_h1_boundaries`` tool."""

    toc_pages: list[int]          # pages identified as TOC (by text content)
    h1_matches: list[H1Match]     # h1 headings and their body pages
    method: Literal["toc_grep", "heading_grep", "none"]
    notes: str = ""

    def cut_candidate_pages(self) -> list[int]:
        """Pages just before each h1 heading page — natural cut points."""
        pages = sorted({m.page for m in self.h1_matches if m.page > 1})
        # Cut before the chapter starts (i.e., end the previous shard at page-1)
        return [p - 1 for p in pages if p > 1]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["h1_matches"] = [m.to_dict() for m in self.h1_matches]
        return data


# ── Cut-point ─────────────────────────────────────────────────────────────────


@dataclass
class CutPoint:
    """A proposed shard boundary produced by the agent."""

    cut_after_page: int
    rationale: str
    anchor_type: Literal["h1_heading", "blank", "sparse", "forced"]
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Shard ──────────────────────────────────────────────────────────────────────


@dataclass
class Shard:
    """Lightweight shard descriptor.  ``page_offset`` is used to correct
    ``page_nums`` in sub-PDF DataFrames back to the original document's
    page numbering."""

    page_start: int   # 1-based, inclusive
    page_end: int     # 1-based, inclusive
    page_offset: int  # = page_start - 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── PageMap ───────────────────────────────────────────────────────────────────


@dataclass
class PageMap:
    """Complete anatomy report produced by ``DocumentAnatomyAgent``.

    Downstream consumers:
    - ``formats/pdf/parser.py`` — decides whether to physically split.
    - ``structure/layout_parser.py`` (future) — page labels annotate headings.
    - Page Memory (future) — ``page_features`` become the structural component.
    """

    job_id: str
    file_path: str
    page_count: int
    h1_result: H1BoundaryResult
    page_features: list[PageFeature]
    shards: list[Shard]
    needs_split: bool
    global_signals: dict[str, Any]
    agent_decision_log: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    def page_feature_map(self) -> dict[int, PageFeature]:
        return {pf.page: pf for pf in self.page_features}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "file_path": self.file_path,
            "page_count": self.page_count,
            "needs_split": self.needs_split,
            "h1_result": self.h1_result.to_dict(),
            "shards": [s.to_dict() for s in self.shards],
            "global_signals": self.global_signals,
            "agent_decision_log": self.agent_decision_log,
            "page_features": [pf.to_dict() for pf in self.page_features],
            "created_at": self.created_at.isoformat(),
        }
