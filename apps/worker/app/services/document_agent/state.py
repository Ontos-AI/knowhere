"""State carried by the document profile workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.document_agent.manifest import (
    DocumentProfile,
    PageFeature,
    PageLabel,
    ProfileVerdict,
    ShardPlan,
    TocAnchorPage,
    TocResult,
)


class ProfileState(str, Enum):
    INIT = "init"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


@dataclass
class ProfileBlackboard:
    page_count: int = 0
    document_profile: DocumentProfile | None = None
    page_features: list[PageFeature] = field(default_factory=list)
    page_labels: list[PageLabel] = field(default_factory=list)
    doc_stats: dict[str, Any] = field(default_factory=dict)
    extrema_pages: list[int] = field(default_factory=list)
    toc_anchor_pages: list[TocAnchorPage] = field(default_factory=list)
    pdf_outline_roots: list[dict[str, Any]] | None = None
    toc_result: TocResult | None = None
    toc_hierarchies: list[dict[str, Any]] | None = None
    skeleton_anchor: dict[str, Any] | None = None
    skeleton_nodes: list[dict[str, Any]] | None = None
    pending_skeleton_anchors: list[dict[str, Any]] = field(default_factory=list)
    shard_plan: ShardPlan | None = None
    validation_report: dict[str, Any] | None = None
    verdict: ProfileVerdict | None = None
    # Values are PageTextBands (or legacy plain str / {"content","header","footer"}).
    page_full_text_cache: dict[int, Any] = field(default_factory=dict)
    # Optional temporary grep view after text.strip_*; None means use each page's content.
    page_text_search_view: dict[int, str] | None = None
    global_signals: dict[str, Any] = field(default_factory=dict)
