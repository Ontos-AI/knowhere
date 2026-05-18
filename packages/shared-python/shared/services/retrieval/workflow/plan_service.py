"""Plan loading and creation for decomposed retrieval workflows."""
from __future__ import annotations

from loguru import logger

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.cache_service import (
    get_cached_workflow_plan,
    set_cached_workflow_plan,
)
from shared.services.retrieval.llm_adapter import LLMFn
from shared.services.retrieval.workflow.planner import QueryPlanner
from shared.services.retrieval.workflow.types import QueryPlan


class WorkflowPlanService:
    async def load_or_create(
        self,
        *,
        user_id: str,
        namespace: str,
        query: str,
        planner_llm: LLMFn | None,
        planner_ledger: BudgetLedger,
        max_steps: int,
        wallet_total: int,
        per_retrieve: int,
        corpus_total_docs: int,
        corpus_total_chunks: int,
    ) -> QueryPlan:
        try:
            cached = await get_cached_workflow_plan(user_id=user_id, namespace=namespace, query=query)
            if cached:
                return QueryPlan.from_dict(cached, original_query=query)
        except Exception as exc:
            logger.warning(f"workflow plan cache read failed (ignored): {exc}")

        planner = QueryPlanner(
            llm_fn=planner_llm,
            planner_ledger=planner_ledger,
            max_steps=max_steps,
            total_budget=wallet_total,
            per_step_budget=per_retrieve,
        )
        plan = await planner.plan(
            query=query,
            corpus_total_docs=corpus_total_docs,
            corpus_total_chunks=corpus_total_chunks,
        )
        try:
            await set_cached_workflow_plan(
                user_id=user_id,
                namespace=namespace,
                query=query,
                plan=plan.to_dict(),
            )
        except Exception as exc:
            logger.warning(f"workflow plan cache write failed (ignored): {exc}")
        return plan
