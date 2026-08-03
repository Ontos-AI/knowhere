from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.services.retrieval.execution import routes as route_module
from shared.services.retrieval.execution.reference_resolver import (
    ResolvedWorkflowReferences,
)
from shared.services.retrieval.execution.route_types import RetrievalRouteContext
from shared.services.retrieval.llm_adapter import LLMFn, create_retrieval_planner_fn
from shared.services.retrieval.workflow.orchestrator import DbSessionFactory, WorkflowOrchestrator
from shared.services.retrieval.workflow.plan_service import WorkflowPlanService
from shared.services.retrieval.workflow.planner import QueryPlanner
from shared.services.retrieval.workflow.run_request import WorkflowRunRequest
from shared.services.retrieval.workflow.step_runner import WorkflowStepRunner
from shared.services.retrieval.workflow.types import (
    PlannedStep,
    QueryPlan,
    StepResult,
    WorkflowResult,
)

RouteRow = dict[str, object]


def _build_planner(
    llm_fn: LLMFn,
    *,
    timeout_seconds: float = 1.0,
) -> QueryPlanner:
    return QueryPlanner(
        llm_fn=llm_fn,
        planner_ledger=None,
        max_steps=3,
        total_budget=100,
        per_step_budget=10,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_workflow_planner_timeout_should_return_single_step_fallback() -> None:
    async def slow_llm(_prompt: object) -> str:
        await asyncio.sleep(0.05)
        return "{}"

    planner = _build_planner(slow_llm, timeout_seconds=0.001)

    plan = await planner.plan(query="original query")

    assert plan.planner_status == "fallback"
    assert plan.steps[0].sub_query == "original query"
    assert plan.planner_error is not None
    assert "timed out" in plan.planner_error


@pytest.mark.asyncio
async def test_workflow_planner_provider_error_should_return_single_step_fallback() -> None:
    async def failing_llm(_prompt: object) -> str:
        raise LLMServiceException(internal_message="provider unavailable")

    planner = _build_planner(failing_llm)

    plan = await planner.plan(query="provider failure query")

    assert plan.planner_status == "fallback"
    assert plan.steps[0].sub_query == "provider failure query"
    assert plan.planner_error is not None


@pytest.mark.asyncio
async def test_workflow_planner_invalid_json_should_return_single_step_fallback() -> None:
    async def invalid_llm(_prompt: object) -> str:
        return "not json"

    planner = _build_planner(invalid_llm)

    plan = await planner.plan(query="invalid planner output")

    assert plan.planner_status == "fallback"
    assert plan.steps[0].sub_query == "invalid planner output"
    assert plan.planner_error is not None
    assert "JSON" in plan.planner_error


@pytest.mark.asyncio
async def test_workflow_planner_unexpected_code_error_should_propagate() -> None:
    async def buggy_llm(_prompt: object) -> str:
        raise RuntimeError("unexpected planner bug")

    planner = _build_planner(buggy_llm)

    with pytest.raises(RuntimeError, match="unexpected planner bug"):
        await planner.plan(query="buggy planner query")


@pytest.mark.asyncio
async def test_workflow_planner_llm_should_pass_timeout_to_provider_client(
    monkeypatch: MonkeyPatch,
) -> None:
    observed_timeouts: list[object] = []

    class FakeClient:
        def chat_completion_with_usage(
            self,
            _prompt: object,
            **kwargs: object,
        ) -> tuple[str, dict[str, int]]:
            observed_timeouts.append(kwargs.get("timeout"))
            return "{}", {"total_tokens": 1}

    def fake_build_client_for_channel(
        *,
        channel: str,
        model: str,
    ) -> tuple[FakeClient, str]:
        assert channel == "text"
        return FakeClient(), model

    monkeypatch.setattr(
        "shared.services.retrieval.llm_adapter._has_llm_credentials",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.llm_adapter._build_client_for_channel",
        fake_build_client_for_channel,
    )

    planner_llm = create_retrieval_planner_fn(timeout_seconds=2.1)

    assert planner_llm is not None
    await planner_llm("timeout contract")
    assert observed_timeouts == [3]


@pytest.mark.asyncio
async def test_workflow_inventory_session_should_close_before_planner_starts(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    inventory_db = object()

    @asynccontextmanager
    async def fake_db_factory() -> AsyncGenerator[AsyncSession, None]:
        events.append("inventory_open")
        try:
            yield cast(AsyncSession, inventory_db)
        finally:
            events.append("inventory_close")

    async def fake_load_budget_inventory(
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        exclude_document_ids: list[str],
    ) -> tuple[int, int, dict[str, int]]:
        assert db is inventory_db
        assert user_id == "contract-user"
        assert namespace == "contract-namespace"
        assert exclude_document_ids == []
        events.append("inventory_loaded")
        return 3, 2, {}

    class RecordingPlanService:
        async def load_or_create(self, **kwargs: object) -> QueryPlan:
            events.append("planner_start")
            assert events == [
                "inventory_open",
                "inventory_loaded",
                "inventory_close",
                "planner_start",
            ]
            return QueryPlan.single_step(str(kwargs["query"]))

    class RecordingStepRunner:
        async def run_step(self, **kwargs: object) -> None:
            step = cast(PlannedStep, kwargs["step"])
            results_by_id = cast(dict[str, StepResult], kwargs["results_by_id"])
            results_by_id[step.id] = StepResult(
                step_id=step.id,
                sub_query=step.sub_query,
                step_kind=step.step_kind,
                depends_on=step.depends_on,
                output_role=step.output_role,
                status="done",
                answer_text="",
            )

    def create_step_runner(
        db_factory: DbSessionFactory,
        parent_run_id: str,
    ) -> WorkflowStepRunner:
        assert db_factory is fake_db_factory
        assert parent_run_id
        return cast(WorkflowStepRunner, RecordingStepRunner())

    monkeypatch.setattr(
        "shared.services.retrieval.workflow.orchestrator._load_budget_inventory",
        fake_load_budget_inventory,
    )
    orchestrator = WorkflowOrchestrator(
        db_factory=fake_db_factory,
        plan_service=cast(WorkflowPlanService, RecordingPlanService()),
        step_runner_factory=create_step_runner,
    )

    await orchestrator.run_request(
        cast(AsyncSession, object()),
        request=WorkflowRunRequest(
            user_id="contract-user",
            namespace="contract-namespace",
            query="session lifetime",
            top_k=1,
            exclude_document_ids=[],
            exclude_sections=[],
        ),
        llm_fn=None,
    )

    assert events[:4] == [
        "inventory_open",
        "inventory_loaded",
        "inventory_close",
        "planner_start",
    ]


@pytest.mark.asyncio
async def test_agentic_route_should_release_route_session_before_fresh_final_hydration(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    route_db = _RecordingRouteSession(events)
    fresh_db = object()

    @asynccontextmanager
    async def fake_get_db_context() -> AsyncGenerator[AsyncSession, None]:
        events.append("fresh_db_open")
        try:
            yield cast(AsyncSession, fresh_db)
        finally:
            events.append("fresh_db_close")

    class FakeWorkflowOrchestrator:
        async def run_request(
            self,
            db: AsyncSession,
            *,
            request: WorkflowRunRequest,
        ) -> WorkflowResult:
            events.append("workflow_start")
            assert db is route_db
            assert events[:2] == ["route_rollback", "workflow_start"]
            return WorkflowResult(
                namespace=request.namespace,
                query=request.query,
                router_used="workflow_single_step",
                answer_text="",
                referenced_chunks=[
                    {
                        "chunk_id": "chunk_contract",
                        "document_id": "doc_contract",
                        "chunk_type": "text",
                        "section_path": "contract/section",
                        "file_path": None,
                        "job_id": "job_contract",
                    }
                ],
            )

    async def fake_resolve_workflow_references(
        *,
        db: AsyncSession,
        user_id: str,
        namespace: str,
        refs: list[RouteRow],
        score_by_chunk_id: dict[str, float] | None = None,
    ) -> ResolvedWorkflowReferences:
        assert db is fresh_db
        assert user_id == "contract-user"
        assert namespace == "contract-namespace"
        assert score_by_chunk_id is None
        events.append("resolve_references")
        row = {
            "document_id": "doc_contract",
            "chunk_id": "chunk_contract",
            "source_file_name": "contract.pdf",
            "section_path": "contract/section",
            "chunk_type": "text",
            "content": "contract content",
        }
        return ResolvedWorkflowReferences(refs=refs, rows=[row])

    async def fake_assemble_retrieval_results(
        *,
        db: AsyncSession,
        rows: list[RouteRow],
        exclude_document_ids: list[str],
        exclude_sections: list[dict[str, str]],
        allowed_chunk_types: set[str] | None,
    ) -> list[RouteRow]:
        assert db is fresh_db
        assert exclude_document_ids == []
        assert exclude_sections == []
        assert allowed_chunk_types is None
        events.append("assemble_results")
        return rows

    monkeypatch.setattr(
        "shared.services.retrieval.workflow.orchestrator.WorkflowOrchestrator",
        FakeWorkflowOrchestrator,
    )
    monkeypatch.setattr(route_module, "open_fresh_database_context", fake_get_db_context)
    monkeypatch.setattr(
        route_module,
        "resolve_workflow_references",
        fake_resolve_workflow_references,
    )
    monkeypatch.setattr(
        route_module,
        "assemble_retrieval_results",
        fake_assemble_retrieval_results,
    )

    outcome = await route_module._run_agentic_route(
        RetrievalRouteContext(
            db=cast(AsyncSession, route_db),
            user_id="contract-user",
            namespace="contract-namespace",
            query="session lifetime",
            top_k=1,
            exclude_document_ids=[],
            exclude_sections=[],
            allowed_chunk_types=None,
            chunk_types=None,
            signal_paths=None,
            filter_mode="delete",
            channels=None,
            channel_weights=None,
            rerank=False,
            threshold=0.0,
            internal_recall_k=None,
            effective_recall_k=3,
            use_agentic=True,
        )
    )

    assert events == [
        "route_rollback",
        "workflow_start",
        "fresh_db_open",
        "resolve_references",
        "assemble_results",
        "fresh_db_close",
    ]
    assert outcome.response["results"][0]["citation"]["document_id"] == "doc_contract"


class _RecordingRouteSession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def rollback(self) -> None:
        self._events.append("route_rollback")
