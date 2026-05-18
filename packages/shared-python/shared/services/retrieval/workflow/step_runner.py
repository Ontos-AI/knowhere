"""Step execution for decomposed retrieval workflows."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.agentic.orchestrator import RetrievalAgent
from shared.services.retrieval.agentic.core.types import AgenticResult
from shared.services.retrieval.llm_adapter import LLMFn
from shared.services.retrieval.workflow.reference_projection import WorkflowReferenceProjection
from shared.services.retrieval.workflow.run_request import WorkflowStepRequest
from shared.services.retrieval.workflow.synthesizer import synthesize_step
from shared.services.retrieval.workflow.types import PlannedStep, StepResult, StepStatus

DbSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
RetrievalAgentFactory = Callable[[], RetrievalAgent]


class WorkflowStepRunner:
    def __init__(
        self,
        *,
        db_factory: DbSessionFactory,
        parent_run_id: str,
        agent_factory: RetrievalAgentFactory | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._parent_run_id = parent_run_id
        self._agent_factory = agent_factory or RetrievalAgent
        self._references = WorkflowReferenceProjection()

    async def run_step(
        self,
        *,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        semaphore: asyncio.Semaphore,
        request: WorkflowStepRequest,
        llm_fn: LLMFn | None,
    ) -> None:
        async with semaphore:
            if step.step_kind == "synthesize":
                await self._run_synthesize_step(step, ledger, results_by_id, llm_fn)
                return
            await self._run_retrieve_step(
                step=step,
                ledger=ledger,
                results_by_id=results_by_id,
                request=request,
                llm_fn=llm_fn,
            )

    async def _run_retrieve_step(
        self,
        *,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        request: WorkflowStepRequest,
        llm_fn: LLMFn | None,
    ) -> None:
        try:
            async with self._db_factory() as step_db:
                agentic_result = await self._agent_factory().run(
                    step_db,
                    user_id=request.user_id,
                    namespace=request.namespace,
                    query=request.query,
                    top_k=request.top_k,
                    llm_fn=llm_fn,
                    exclude_document_ids=request.exclude_document_ids,
                    exclude_sections=request.exclude_sections,
                    data_type=request.data_type,
                    signal_paths=request.signal_paths,
                    filter_mode=request.filter_mode,
                    channels=request.channels,
                    channel_weights=request.channel_weights,
                    internal_recall_k=request.internal_recall_k,
                    ledger=ledger,
                    parent_run_id=self._parent_run_id,
                    workflow_step_id=step.id,
                )
            results_by_id[step.id] = _step_result_from_agentic(step, agentic_result)
        except Exception as exc:
            logger.exception(f"workflow retrieve step failed: step_id={step.id}")
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status="error",
                error=str(exc),
                budget_snapshot=ledger.snapshot(),
            )

    async def _run_synthesize_step(
        self,
        step: PlannedStep,
        ledger: BudgetLedger,
        results_by_id: dict[str, StepResult],
        llm_fn: LLMFn | None,
    ) -> None:
        if llm_fn is None:
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status="skipped",
                answer_text="",
                error="llm unavailable for synthesis",
                budget_snapshot=ledger.snapshot(),
            )
            return
        prior = {dep: results_by_id[dep] for dep in step.depends_on if dep in results_by_id}
        try:
            answer = await synthesize_step(step, prior_results=prior, llm_fn=llm_fn, ledger=ledger)
            refs = self._references.dedupe(ref for result in prior.values() for ref in result.referenced_chunks)
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status="done",
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
                status="budget_stop" if "budget" in str(exc).lower() else "error",
                answer_text="(budget exhausted)" if "budget" in str(exc).lower() else "",
                error=str(exc),
                budget_snapshot=ledger.snapshot(),
            )


def _step_result_from_agentic(step: PlannedStep, result: AgenticResult) -> StepResult:
    if result.answer_text:
        status: StepStatus = "done"
    elif result.failure_reason:
        status = "not_found"
    elif "budget" in (result.stop_reason or ""):
        status = "budget_stop"
    else:
        status = "done"
    return StepResult(
        step_id=step.id,
        sub_query=step.sub_query,
        step_kind=step.step_kind,
        depends_on=step.depends_on,
        output_role=step.output_role,
        status=status,
        answer_text=result.answer_text,
        evidence_text=result.evidence_text,
        referenced_chunks=result.referenced_chunks,
        budget_snapshot=result.budget_snapshot,
        router_used=result.router_used,
        stop_reason=result.stop_reason,
        failure_reason=result.failure_reason,
    )
