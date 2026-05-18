"""Runtime setup helpers for agentic retrieval."""
from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk
from shared.services.retrieval.agentic.budget import BudgetExceeded, BudgetPoolName
from shared.services.retrieval.agentic.types import AgentRunConfig, AgentState
from shared.services.retrieval.llm_adapter import LLMFn, current_llm_usage
from shared.utils.token_estimate import estimate_tokens


def build_config_from_env() -> AgentRunConfig:
    return AgentRunConfig(
        max_revisions=int(os.environ.get("RETRIEVAL_AGENTIC_MAX_REVISIONS", "2")),
        max_nav_depth=int(os.environ.get("RETRIEVAL_AGENTIC_MAX_NAV_DEPTH", "3")),
        latency_budget_ms=int(os.environ.get("RETRIEVAL_AGENTIC_LATENCY_BUDGET_MS", "12000")),
        token_budget_total=int(os.environ.get("RETRIEVAL_AGENTIC_TOKEN_BUDGET_TOTAL", "40000")),
        planning_ratio=float(os.environ.get("RETRIEVAL_AGENTIC_PLANNING_RATIO", "0.5")),
        bootstrap_budget=int(os.environ.get("RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET", "2000")),
        per_doc_min_share=int(os.environ.get("RETRIEVAL_AGENTIC_PER_DOC_MIN_SHARE", "1500")),
        inventory_aware=os.environ.get("RETRIEVAL_AGENTIC_INVENTORY_AWARE", "true") == "true",
    )


async def load_budget_inventory(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
) -> tuple[int, int, dict[str, int]]:
    stmt = (
        select(Document.document_id, func.count(DocumentChunk.id))
        .join(
            DocumentChunk,
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id),
        )
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .group_by(Document.document_id)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))

    result = await db.execute(stmt)
    doc_chunks = {str(doc_id): int(count or 0) for doc_id, count in result.all()}
    return sum(doc_chunks.values()), len(doc_chunks), doc_chunks


class AgentLlmBudget:
    def __init__(self, state: AgentState) -> None:
        self._state = state

    async def call(
        self,
        llm_fn: LLMFn,
        prompt: Any,
        *,
        pool: BudgetPoolName,
        doc_id: str | None = None,
        priority: str = "normal",
    ) -> str:
        ledger = self._state.ledger
        if ledger is None:
            return await llm_fn(prompt)

        prompt_text = _stringify_llm_input(prompt)
        est = estimate_tokens(prompt_text)
        reserved = await ledger.try_reserve(
            pool,
            est,
            doc_id=doc_id,
            priority="low" if priority == "low" else "normal",
        )
        if not reserved:
            raise BudgetExceeded(f"{pool} budget exhausted")

        try:
            response = await llm_fn(prompt)
        except Exception:
            await ledger.refund(pool, est=est, doc_id=doc_id)
            raise

        usage = current_llm_usage.get() or {}
        actual = int(usage.get("prompt_tokens") or est)
        await ledger.commit(pool, actual=actual, est=est, doc_id=doc_id)
        return response

    def for_pool(self, llm_fn: LLMFn, *, pool: BudgetPoolName) -> LLMFn:
        async def _call(prompt: Any) -> str:
            return await self.call(llm_fn, prompt, pool=pool)

        return _call

    def for_document(
        self,
        llm_fn: LLMFn,
        *,
        doc_id: str,
        depth: int,
    ) -> LLMFn:
        async def _call(prompt: Any) -> str:
            return await self.call(
                llm_fn,
                prompt,
                pool="planning",
                doc_id=doc_id,
                priority="low" if depth >= 2 else "normal",
            )

        return _call

    def for_discovery(
        self,
        llm_fn: LLMFn,
        *,
        doc_id: str,
        low_priority: bool,
    ) -> LLMFn:
        async def _call(prompt: Any) -> str:
            return await self.call(
                llm_fn,
                prompt,
                pool="planning",
                doc_id=doc_id,
                priority="low" if low_priority else "normal",
            )

        return _call


def _stringify_llm_input(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt
    try:
        return json.dumps(prompt, ensure_ascii=False, default=str)
    except Exception:
        return str(prompt)
