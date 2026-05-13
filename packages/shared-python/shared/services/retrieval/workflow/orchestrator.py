"""Workflow orchestrator for decomposed retrieval queries."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db_context
from shared.services.retrieval.agentic.budget import BudgetLedger
from shared.services.retrieval.agentic.orchestrator import RetrievalAgent, _load_budget_inventory
from shared.services.retrieval.agentic.types import AgenticResult
from shared.services.retrieval.cache_service import (
    get_cached_workflow_plan,
    set_cached_workflow_plan,
)
from shared.services.retrieval.llm_adapter import (
    create_retrieval_llm_fn,
    create_retrieval_planner_fn,
)
from shared.services.retrieval.workflow.planner import QueryPlanner
from shared.services.retrieval.workflow.synthesizer import compose_final_answer, synthesize_step
from shared.services.retrieval.workflow.types import PlannedStep, QueryPlan, StepResult, WorkflowResult
from shared.services.retrieval.workflow.wallet import BudgetWallet


class WorkflowOrchestrator:
    """Plan and execute a query workflow DAG."""

    def __init__(self) -> None:
        self.parent_run_id = f'wret_{uuid4().hex[:12]}'

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
        llm_fn=None,
    ) -> WorkflowResult:
        t0 = time.monotonic()
        llm_fn = llm_fn or create_retrieval_llm_fn()
        planner_llm = create_retrieval_planner_fn(thinking=True)
        planner_budget = _env_int('RETRIEVAL_PLANNER_THINKING_BUDGET', 4000)
        wallet_total = _env_int('RETRIEVAL_WALLET_TOTAL_BUDGET', 200000)
        per_retrieve = _env_int('RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET', 40000)
        per_synthesize = _env_int('RETRIEVAL_WALLET_PER_SYNTHESIZE_STEP_BUDGET', 6000)
        max_steps = _env_int('RETRIEVAL_DECOMPOSITION_MAX_STEPS', 5)

        planner_ledger = BudgetLedger(
            total=planner_budget,
            planning_ratio=0.0,
            bootstrap=planner_budget,
            per_doc_min_share=0,
        )
        total_chunks, total_docs, _chunks_count_by_doc = await _load_budget_inventory(
            db,
            user_id=user_id,
            namespace=namespace,
            exclude_document_ids=exclude_document_ids,
        )
        planner_ledger.total_chunks = total_chunks
        planner_ledger.total_docs = total_docs
        plan = await self._load_or_plan(
            user_id=user_id,
            namespace=namespace,
            query=query,
            planner_llm=planner_llm,
            planner_ledger=planner_ledger,
            max_steps=max_steps,
            wallet_total=wallet_total,
            per_retrieve=per_retrieve,
            kb_total_docs=total_docs,
            kb_total_chunks=total_chunks,
        )

        wallet = BudgetWallet(
            total=wallet_total,
            per_retrieve_step_default=per_retrieve,
            per_synthesize_step_default=per_synthesize,
        )
        ledgers = await wallet.allocate(plan)
        results_by_id: dict[str, StepResult] = {}
        sem = asyncio.Semaphore(_env_int('RETRIEVAL_WORKFLOW_PARALLEL_MAX', 3))

        for batch in plan.topological_batches():
            await asyncio.gather(
                *[
                    self._run_step(
                        db,
                        step=step,
                        ledger=ledgers[step.id],
                        results_by_id=results_by_id,
                        semaphore=sem,
                        user_id=user_id,
                        namespace=namespace,
                        top_k=step.top_k or top_k,
                        exclude_document_ids=exclude_document_ids,
                        exclude_sections=exclude_sections,
                        data_type=step.data_type or data_type,
                        signal_paths=signal_paths,
                        filter_mode=filter_mode,
                        channels=channels,
                        channel_weights=channel_weights,
                        llm_fn=llm_fn,
                    )
                    for step in batch
                ]
            )
            for step in batch:
                await wallet.reclaim(step.id, ledgers[step.id])

        answer_text = compose_final_answer(plan, results_by_id)
        ordered_results = [results_by_id[step.id] for step in plan.steps if step.id in results_by_id]
        referenced_chunks = _dedupe_references(
            ref for step_result in ordered_results for ref in step_result.referenced_chunks
        )
        api_results = _references_to_results(referenced_chunks)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            'workflow retrieval DONE: steps={} refs={} answer_chars={} elapsed={}ms',
            len(ordered_results),
            len(referenced_chunks),
            len(answer_text),
            elapsed_ms,
        )
        return WorkflowResult(
            namespace=namespace,
            query=query,
            router_used='workflow_decomposed' if len(plan.steps) > 1 else 'workflow_single_step',
            answer_text=answer_text,
            plan=plan,
            steps=ordered_results,
            referenced_chunks=referenced_chunks,
            results=api_results,
            final_strategy_used=plan.final_strategy,
            wallet_snapshot=wallet.snapshot(),
            planner_snapshot=planner_ledger.snapshot(),
            parent_run_id=self.parent_run_id,
        )

    async def _load_or_plan(
        self,
        *,
        user_id: str,
        namespace: str,
        query: str,
        planner_llm,
        planner_ledger: BudgetLedger,
        max_steps: int,
        wallet_total: int,
        per_retrieve: int,
        kb_total_docs: int,
        kb_total_chunks: int,
    ) -> QueryPlan:
        try:
            cached = await get_cached_workflow_plan(user_id=user_id, namespace=namespace, query=query)
            if cached:
                return QueryPlan.from_dict(cached, original_query=query)
        except Exception as exc:
            logger.warning(f'workflow plan cache read failed (ignored): {exc}')

        planner = QueryPlanner(
            llm_fn=planner_llm,
            planner_ledger=planner_ledger,
            max_steps=max_steps,
            total_budget=wallet_total,
            per_step_budget=per_retrieve,
        )
        plan = await planner.plan(
            query=query,
            kb_total_docs=kb_total_docs,
            kb_total_chunks=kb_total_chunks,
        )
        try:
            await set_cached_workflow_plan(
                user_id=user_id,
                namespace=namespace,
                query=query,
                plan=plan.to_dict(),
            )
        except Exception as exc:
            logger.warning(f'workflow plan cache write failed (ignored): {exc}')
        return plan

    async def _run_step(
        self,
        db: AsyncSession,
        *,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        semaphore: asyncio.Semaphore,
        user_id: str,
        namespace: str,
        top_k: int,
        exclude_document_ids: list[str],
        exclude_sections: list[dict[str, str]],
        data_type: int,
        signal_paths: list[str] | None,
        filter_mode: str,
        channels: list[str] | None,
        channel_weights: dict[str, float] | None,
        llm_fn,
    ) -> None:
        async with semaphore:
            if step.step_kind == 'synthesize':
                await self._run_synthesize_step(step, ledger, results_by_id, llm_fn)
                return
            await self._run_retrieve_step(
                db,
                step=step,
                ledger=ledger,
                results_by_id=results_by_id,
                user_id=user_id,
                namespace=namespace,
                top_k=top_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                data_type=data_type,
                signal_paths=signal_paths,
                filter_mode=filter_mode,
                channels=channels,
                channel_weights=channel_weights,
                llm_fn=llm_fn,
            )

    async def _run_retrieve_step(
        self,
        db: AsyncSession,
        *,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        user_id: str,
        namespace: str,
        top_k: int,
        exclude_document_ids: list[str],
        exclude_sections: list[dict[str, str]],
        data_type: int,
        signal_paths: list[str] | None,
        filter_mode: str,
        channels: list[str] | None,
        channel_weights: dict[str, float] | None,
        llm_fn,
    ) -> None:
        try:
            # AsyncSession is not safe for concurrent use.  Workflow steps may
            # run in the same topological batch, so each retrieve step opens an
            # isolated session and leaves the parent session untouched.
            async with get_db_context() as step_db:
                agentic_result = await RetrievalAgent().run(
                    step_db,
                    user_id=user_id,
                    namespace=namespace,
                    query=step.sub_query,
                    top_k=top_k,
                    llm_fn=llm_fn,
                    exclude_document_ids=exclude_document_ids,
                    exclude_sections=exclude_sections,
                    data_type=data_type,
                    signal_paths=signal_paths,
                    filter_mode=filter_mode,
                    channels=channels,
                    channel_weights=channel_weights,
                    ledger=ledger,
                    parent_run_id=self.parent_run_id,
                    workflow_step_id=step.id,
                )
            results_by_id[step.id] = _step_result_from_agentic(step, agentic_result)
        except Exception as exc:
            logger.exception(f'workflow retrieve step failed: step_id={step.id}')
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status='error',
                error=str(exc),
                budget_snapshot=ledger.snapshot(),
            )

    async def _run_synthesize_step(
        self,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        llm_fn,
    ) -> None:
        if llm_fn is None:
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status='skipped',
                answer_text='',
                error='llm unavailable for synthesis',
                budget_snapshot=ledger.snapshot(),
            )
            return
        prior = {dep: results_by_id[dep] for dep in step.depends_on if dep in results_by_id}
        try:
            answer = await synthesize_step(step, prior_results=prior, llm_fn=llm_fn, ledger=ledger)
            refs = _dedupe_references(
                ref for result in prior.values() for ref in result.referenced_chunks
            )
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status='done',
                answer_text=answer,
                referenced_chunks=refs,
                budget_snapshot=ledger.snapshot(),
            )
        except Exception as exc:
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status='budget_stop' if 'budget' in str(exc).lower() else 'error',
                answer_text='(budget exhausted)' if 'budget' in str(exc).lower() else '',
                error=str(exc),
                budget_snapshot=ledger.snapshot(),
            )


def _step_result_from_agentic(step: PlannedStep, result: AgenticResult) -> StepResult:
    if result.answer_text:
        status = 'done'
    elif result.failure_reason:
        status = 'not_found'
    elif 'budget' in (result.stop_reason or ''):
        status = 'budget_stop'
    else:
        status = 'done'
    return StepResult(
        step_id=step.id,
        sub_query=step.sub_query,
        step_kind=step.step_kind,
        depends_on=step.depends_on,
        output_role=step.output_role,
        status=status,  # type: ignore[arg-type]
        answer_text=result.answer_text,
        evidence_text=result.evidence_text,
        referenced_chunks=result.referenced_chunks,
        budget_snapshot=result.budget_snapshot,
        router_used=result.router_used,
        stop_reason=result.stop_reason,
        failure_reason=result.failure_reason,
    )


def _dedupe_references(refs) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ref in refs:
        chunk_id = str(ref.get('chunk_id') or '')
        key = chunk_id or str(ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(ref))
    return out


def _references_to_results(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'chunk_id': ref.get('chunk_id'),
            'document_id': ref.get('document_id'),
            'chunk_type': ref.get('chunk_type'),
            'source': {
                'document_id': ref.get('document_id'),
                'section_path': ref.get('section_path'),
            },
        }
        for ref in refs
    ]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
