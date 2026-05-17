from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


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
    data_type: int
    signal_paths: list[str] | None
    filter_mode: str
    channels: list[str] | None
    channel_weights: dict[str, float] | None
    threshold: float
    effective_recall_k: int
    use_agentic: bool | None


@dataclass(frozen=True)
class RetrievalRouteOutcome:
    response: dict[str, Any]
    hit_stats_results: list[dict[str, Any]]
    completion_label: str
    completion_count: int
    completion_detail: str
