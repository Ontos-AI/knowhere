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
    toc_result: TocResult | None = None
    toc_hierarchies: list[dict[str, Any]] | None = None
    skeleton_anchor: dict[str, Any] | None = None
    skeleton_nodes: list[dict[str, Any]] | None = None
    pending_skeleton_anchors: list[dict[str, Any]] = field(default_factory=list)
    shard_plan: ShardPlan | None = None
    validation_report: dict[str, Any] | None = None
    verdict: ProfileVerdict | None = None
    page_full_text_cache: dict[int, str] = field(default_factory=dict)
    global_signals: dict[str, Any] = field(default_factory=dict)
