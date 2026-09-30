from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.llm_config import LLMConfig
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.execution.plan import (
    run_retrieval_query as execute_retrieval_query,
)

__all__ = ["run_retrieval_query"]


async def run_retrieval_query(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    include_document_ids: list[str] | None = None,
    chunk_types: set[str] | None = None,
        signal_paths: list[str] | None = None,
        filter_mode: str = "delete",
        rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
    use_agentic: bool | None = None,
    agent_explore_model: str | None = None,
    conversation_id: str | None = None,
    llm_config: LLMConfig | None = None,
) -> dict[str, Any]:
    return await execute_retrieval_query(
        db=db,
        user_id=user_id,
        namespace=normalize_retrieval_namespace(namespace),
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        include_document_ids=include_document_ids,
        chunk_types=chunk_types,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        rerank=rerank,
        threshold=threshold,
        internal_recall_k=internal_recall_k,
        use_agentic=use_agentic,
        agent_explore_model=agent_explore_model,
        conversation_id=conversation_id,
        llm_config=llm_config,
    )
