from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.execution.route_types import RetrievalRouteContext
from shared.services.retrieval.settings import (
    INTERNAL_RECALL_K_MULTIPLIER,
    resolve_allowed_chunk_types,
)


@dataclass(frozen=True)
class RetrievalQuery:
    db: AsyncSession
    user_id: str
    namespace: str
    query: str
    top_k: int
    exclude_document_ids: list[str]
    exclude_sections: list[dict[str, str]]
    data_type: int = 1
    signal_paths: list[str] | None = None
    filter_mode: str = "delete"
    channels: list[str] | None = None
    channel_weights: dict[str, float] | None = None
    rerank: bool = False
    threshold: float = 0.0
    internal_recall_k: int | None = None
    use_agentic: bool | None = None

    @classmethod
    def from_parameters(
        cls,
        *,
        db: AsyncSession,
        user_id: str,
        namespace: str,
        query: str,
        top_k: int,
        exclude_document_ids: list[str],
        exclude_sections: list[dict[str, str]],
        data_type: int = 1,
        signal_paths: list[str] | None = None,
        filter_mode: str = "delete",
        channels: list[str] | None = None,
        channel_weights: dict[str, float] | None = None,
        rerank: bool = False,
        threshold: float = 0.0,
        internal_recall_k: int | None = None,
        use_agentic: bool | None = None,
    ) -> "RetrievalQuery":
        return cls(
            db=db,
            user_id=user_id,
            namespace=normalize_retrieval_namespace(namespace),
            query=str(query).strip(),
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            data_type=data_type,
            signal_paths=signal_paths,
            filter_mode=filter_mode,
            channels=channels,
            channel_weights=channel_weights,
            rerank=rerank,
            threshold=threshold,
            internal_recall_k=internal_recall_k,
            use_agentic=use_agentic,
        )

    def build_cache_extra(self) -> dict[str, Any]:
        return {
            "data_type": self.data_type,
            "signal_paths": self.signal_paths,
            "filter_mode": self.filter_mode,
            "channels": self.channels,
            "channel_weights": self.channel_weights,
            "rerank": self.rerank,
            "threshold": self.threshold,
            "internal_recall_k": self.internal_recall_k,
            "decomposition_enabled": True,
        }

    def resolve_allowed_chunk_types(self) -> set[str] | None:
        return resolve_allowed_chunk_types(self.data_type)

    def resolve_effective_recall_k(self) -> int:
        if self.internal_recall_k is not None:
            return self.internal_recall_k
        return self.top_k * INTERNAL_RECALL_K_MULTIPLIER

    def build_route_context(self) -> RetrievalRouteContext:
        return RetrievalRouteContext(
            db=self.db,
            user_id=self.user_id,
            namespace=self.namespace,
            query=self.query,
            top_k=self.top_k,
            exclude_document_ids=self.exclude_document_ids,
            exclude_sections=self.exclude_sections,
            allowed_chunk_types=self.resolve_allowed_chunk_types(),
            data_type=self.data_type,
            signal_paths=self.signal_paths,
            filter_mode=self.filter_mode,
            channels=self.channels,
            channel_weights=self.channel_weights,
            rerank=self.rerank,
            threshold=self.threshold,
            internal_recall_k=self.internal_recall_k,
            effective_recall_k=self.resolve_effective_recall_k(),
            use_agentic=self.use_agentic,
        )
