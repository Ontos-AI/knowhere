"""State carried by the document profile agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.document_agent.manifest import (
    AgentVerdict,
    DocumentProfile,
    H1BoundaryResult,
    PageFeature,
    PageLabel,
    ShardPlan,
    TocAnchorPage,
    TocResult,
)


class DocumentAgentState(str, Enum):
    INIT = "init"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


@dataclass
class AgentBlackboard:
    page_count: int = 0
    document_profile: DocumentProfile | None = None
    page_features: list[PageFeature] = field(default_factory=list)
    page_labels: list[PageLabel] = field(default_factory=list)
    doc_stats: dict[str, Any] = field(default_factory=dict)
    extrema_pages: list[int] = field(default_factory=list)
    toc_anchor_pages: list[TocAnchorPage] = field(default_factory=list)
    toc_result: TocResult | None = None
    toc_hierarchies: list[dict[str, Any]] | None = None
    h1_result: H1BoundaryResult | None = None
    shard_plan: ShardPlan | None = None
    validation_report: dict[str, Any] | None = None
    verdict: AgentVerdict | None = None
    step_history: list[dict[str, Any]] = field(default_factory=list)
    page_full_text_cache: dict[int, str] = field(default_factory=dict)
    global_signals: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

