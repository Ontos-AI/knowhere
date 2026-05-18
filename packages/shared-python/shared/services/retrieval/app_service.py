from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.execution.plan import (
    run_retrieval_query as execute_retrieval_query,
)
from shared.services.retrieval.search.scoring import merge_channels_rrf

__all__ = ["merge_channels_rrf", "run_retrieval_query"]


async def run_retrieval_query(
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
) -> dict[str, Any]:
    return await execute_retrieval_query(
        db=db,
        user_id=user_id,
        namespace=normalize_retrieval_namespace(namespace),
        query=query,
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
