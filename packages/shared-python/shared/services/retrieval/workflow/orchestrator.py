"""Workflow orchestrator for decomposed retrieval queries."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.agentic.orchestrator import _load_budget_inventory
from shared.services.retrieval.llm_adapter import (
    create_retrieval_llm_fn,
    create_retrieval_planner_fn,
)
from shared.services.retrieval.workflow.plan_service import WorkflowPlanService
from shared.services.retrieval.workflow.reference_projection import WorkflowReferenceProjection
from shared.services.retrieval.workflow.run_request import WorkflowRunRequest
from shared.services.retrieval.workflow.runtime_config import WorkflowRuntimeConfig
from shared.services.retrieval.workflow.step_runner import WorkflowStepRunner
from shared.services.retrieval.workflow.types import StepResult, WorkflowResult
from shared.services.retrieval.workflow.wallet import BudgetWallet

DbSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
WorkflowStepRunnerFactory = Callable[[DbSessionFactory, str], WorkflowStepRunner]


def _create_workflow_step_runner(
    db_factory: DbSessionFactory,
    parent_run_id: str,
) -> WorkflowStepRunner:
    return WorkflowStepRunner(db_factory=db_factory, parent_run_id=parent_run_id)


class WorkflowOrchestrator:
    """Plan and execute a query workflow DAG."""

    def __init__(
        self,
        db_factory: DbSessionFactory | None = None,
        plan_service: WorkflowPlanService | None = None,
        step_runner_factory: WorkflowStepRunnerFactory | None = None,
    ) -> None:
        self.parent_run_id = f'wret_{uuid4().hex[:12]}'
        self._db_factory = db_factory
        self._plan_service = plan_service or WorkflowPlanService()
        self._step_runner_factory = (
            step_runner_factory or _create_workflow_step_runner
        )

    def _get_db_factory(self) -> DbSessionFactory:
        if self._db_factory is not None:
            return self._db_factory

        from shared.core.database import get_db_context

        return get_db_context

    async def run(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        top_k: int,
        exclude_document_ids: list[str],
        exclude_sections: list[dict[str, str]],
        data_type: int = 1,
        signal_paths: list[str] | None = None,
        filter_mode: str = 'delete',
        channels: list[str] | None = None,
        channel_weights: dict[str, float] | None = None,
        internal_recall_k: int | None = None,
        rerank: bool = False,
        threshold: float = 0.0,
        llm_fn=None,
    ) -> WorkflowResult:
        request = WorkflowRunRequest(
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            data_type=data_type,
            signal_paths=signal_paths,
            filter_mode=filter_mode,
            channels=channels,
            channel_weights=channel_weights,
            internal_recall_k=internal_recall_k,
            rerank=rerank,
            threshold=threshold,
        )
        return await self.run_request(db, request=request, llm_fn=llm_fn)

    async def run_request(
        self,
        db: AsyncSession,
        *,
        request: WorkflowRunRequest,
        llm_fn=None,
    ) -> WorkflowResult:
        t0 = time.monotonic()
        config = WorkflowRuntimeConfig.from_env()
        llm_fn = llm_fn or create_retrieval_llm_fn()
        planner_llm = create_retrieval_planner_fn(thinking=True)

        planner_ledger = BudgetLedger(
            total=config.planner_budget,
            planning_ratio=0.0,
            bootstrap=config.planner_budget,
            per_doc_min_share=0,
        )
        total_chunks, total_docs, _chunks_count_by_doc = await _load_budget_inventory(
            db,
            user_id=request.user_id,
            namespace=request.namespace,
            exclude_document_ids=request.exclude_document_ids,
        )
        planner_ledger.total_chunks = total_chunks
        planner_ledger.total_docs = total_docs
        plan = await self._plan_service.load_or_create(
            user_id=request.user_id,
            namespace=request.namespace,
            query=request.query,
            top_k=request.top_k,
            data_type=request.data_type,
            exclude_document_ids=request.exclude_document_ids,
            planner_llm=planner_llm,
            planner_ledger=planner_ledger,
            max_steps=config.max_steps,
            wallet_total=config.wallet_total_budget,
            per_retrieve=config.per_retrieve_step_budget,
            corpus_total_docs=total_docs,
            corpus_total_chunks=total_chunks,
        )
        # TODO(retrieval-agentic-nav): redesign this outer workflow as a real
        # observe-act agent that can pass evidence between steps, decide whether
        # to add sub-queries after seeing retrieval results, and own global
        # document selection/stop decisions. The per-document navigator remains
        # a sub-agent and should not take over cross-document orchestration.

        wallet = BudgetWallet(
            total=config.wallet_total_budget,
            per_retrieve_step_default=config.per_retrieve_step_budget,
        )
        ledgers = await wallet.allocate(plan)
        results_by_id: dict[str, StepResult] = {}
        sem = asyncio.Semaphore(config.parallel_max)
        step_runner = self._step_runner_factory(
            self._get_db_factory(),
            self.parent_run_id,
        )

        for batch in plan.topological_batches():
            await asyncio.gather(
                *[
                    step_runner.run_step(
                        step=step,
                        ledger=ledgers[step.id],
                        results_by_id=results_by_id,
                        semaphore=sem,
                        request=request.for_step(step),
                        llm_fn=llm_fn,
                    )
                    for step in batch
                ]
            )
            for step in batch:
                await wallet.reclaim(step.id, ledgers[step.id])

        ordered_results = [results_by_id[step.id] for step in plan.steps if step.id in results_by_id]
        evidence_chars = sum(len(step_result.evidence_text or "") for step_result in ordered_results)
        reference_projection = WorkflowReferenceProjection()
        referenced_chunks = reference_projection.dedupe(
            ref for step_result in ordered_results for ref in step_result.referenced_chunks
        )
        api_results = reference_projection.to_api_results(referenced_chunks)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            'workflow retrieval DONE: steps={} refs={} evidence_chars={} elapsed={}ms',
            len(ordered_results),
            len(referenced_chunks),
            evidence_chars,
            elapsed_ms,
        )
        return WorkflowResult(
            namespace=request.namespace,
            query=request.query,
            router_used='workflow_decomposed' if len(plan.steps) > 1 else 'workflow_single_step',
            answer_text="",
            plan=plan,
            steps=ordered_results,
            referenced_chunks=referenced_chunks,
            results=api_results,
            final_strategy_used=plan.final_strategy,
            wallet_snapshot=wallet.snapshot(),
            planner_snapshot=planner_ledger.snapshot(),
            parent_run_id=self.parent_run_id,
        )
