from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins


@dataclass(frozen=True)
class RetrievalRouteContext:
    db: AsyncSession
    user_id: str
    namespace: str
    query: str
    top_k: int
    exclude_document_ids: list[str]
    exclude_sections: list[dict[str, str]]
    allowed_chunk_types: set[str] | None
    chunk_types: set[str] | None
    signal_paths: list[str] | None
    filter_mode: str
    channels: list[str] | None
    channel_weights: dict[str, float] | None
    rerank: bool
    threshold: float
    internal_recall_k: int | None
    effective_recall_k: int
    use_agentic: bool | None
    conversation_id: str | None = None
    revision_pins: RetrievalRevisionPins | None = None
    document_scope: DocumentScope = DocumentScope()

    def __post_init__(self) -> None:
        # Direct route callers still supply the pre-existing exclude field.
        object.__setattr__(
            self, "document_scope", self.document_scope.excluding(self.exclude_document_ids)
        )


@dataclass(frozen=True)
class RetrievalRouteOutcome:
    response: dict[str, Any]
    hit_stats_results: list[dict[str, Any]]
    completion_label: str
    completion_count: int
    completion_detail: str
