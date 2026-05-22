"""State carried by the document profile agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.services.document_agent.manifest import (
    H1BoundaryResult,
    HierarchyAssistPlan,
    PageFeature,
    PageLabel,
    ShardPlan,
    TocResult,
)


class DocumentAgentState(str, Enum):
    INIT = "init"
    PROBED = "probed"
    CLASSIFIED = "classified"
    H1_FOUND = "h1_found"
    PLANNED = "planned"
    VALIDATED = "validated"
    PERSISTED = "persisted"
    READY = "ready"
    FAILED = "failed"
    PROCESSING_PLAN_PROPOSED = "processing_plan_proposed"


@dataclass
class AgentBlackboard:
    page_count: int = 0
    page_features: list[PageFeature] = field(default_factory=list)
    page_labels: list[PageLabel] = field(default_factory=list)
    toc_result: TocResult | None = None
    h1_result: H1BoundaryResult | None = None
    hierarchy_assist: HierarchyAssistPlan | None = None
    shard_plan: ShardPlan | None = None
    validation_report: dict[str, Any] | None = None
    global_signals: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    state_trace: list[str] = field(default_factory=list)

    def mark(self, state: DocumentAgentState) -> None:
        self.state_trace.append(state.value)
