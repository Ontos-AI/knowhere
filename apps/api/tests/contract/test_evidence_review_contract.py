"""Opt-in review follows the API contract and the exact delivered evidence."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from shared.services.retrieval.agent_explore.evidence_pool import Candidate
    from shared.services.retrieval.agent_explore.types import EpisodeResult
    from shared.services.retrieval.execution.route_types import RetrievalRouteContext


def _report(status: str = "sufficient") -> dict[str, Any]:
    return {
        "version": "evidence-review-v1", "status": status,
        "reason": "The selected source states the requested payload.",
        "coverage": [{
            "facet_id": "F1", "requirement": "Payload to LEO", "status": "supported",
            "citations": [{"evidence_id": "C1", "quote": "23 tonnes"}],
        }] if status == "sufficient" else [],
        "attempts": 1, "repairs": 0, "reviewer_tokens": 17,
        "usage_complete": True, "evidence_fingerprint": "packet-digest",
        "sources": [{
            "evidence_id": "C1", "document_id": "doc_launch",
            "chunk_id": "chunk_launch", "revision": "revision_launch",
            "section_path": "Launch/Capacity", "source_file_name": "launch.pdf",
            "page_nums": [4],
        }],
    }


@pytest.mark.parametrize("api_version", ["v1", "v2"])
async def test_api_review_flag_defaults_off_and_preserves_review_metadata(
    monkeypatch: MonkeyPatch, api_version: str,
) -> None:
    from app.api.v1.routes import retrieval as v1
    from app.api.v2.routes import retrieval as v2
    from app.services.rate_limit.data_structures import CurrentUser

    calls: list[dict[str, Any]] = []

    async def run_query(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "namespace": kwargs["namespace"], "query": kwargs["query"],
            "router_used": "agent_explore", "answer_text": "",
            **({"evidence_review": _report()} if kwargs["review_evidence"] else {}),
        }

    async def no_database() -> AsyncIterator[None]:
        yield None

    app = FastAPI()
    app.include_router((v1 if api_version == "v1" else v2).router, prefix=f"/api/{api_version}/retrieval")
    app.dependency_overrides[v1.with_current_user] = lambda: CurrentUser("reader", "developer")
    app.dependency_overrides[v1.get_db] = no_database
    monkeypatch.setattr(v1, "run_retrieval_query", run_query)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        default_response = await client.post(f"/api/{api_version}/retrieval/query", json={"query": " payload "})
        reviewed_response = await client.post(
            f"/api/{api_version}/retrieval/query",
            json={"query": " payload ", "review_evidence": True},
        )

    assert default_response.status_code == reviewed_response.status_code == 200
    assert [call["review_evidence"] for call in calls] == [False, True]
    assert all(call["query"] == "payload" for call in calls)
    assert default_response.json()["evidence_review"] is None
    assert reviewed_response.json()["evidence_review"] == _report()
    assert reviewed_response.json()["answer_text"] == ""
    request_model = v1.RetrievalQueryRequest if api_version == "v1" else v2.RetrievalQueryRequestV2
    assert request_model.model_json_schema()["properties"]["review_evidence"]["default"] is False


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "document_id": "doc_launch", "chunk_id": "chunk_launch",
        "job_result_id": "revision_launch", "source_file_name": "launch.pdf",
        "section_path": "Launch/Capacity", "chunk_type": "text",
        "content": "Payload: 23 tonnes to LEO.", "sort_order": 0,
        "chunk_metadata": {"page_nums": [4]}, **changes,
    }


def _pool(rows: list[dict[str, Any]]) -> list[Candidate]:
    from shared.services.retrieval.agent_explore.evidence_pool import Candidate

    return [Candidate(
        handle=f"R1.{index}", kind="read", document_id=row["document_id"],
        source_file_name=row["source_file_name"], section_path=row["section_path"],
        chunk_ids=(row["chunk_id"],), summary="Wrong executor summary: 100 tonnes.",
    ) for index, row in enumerate(rows, start=1)]


class _FinishHarness:
    def __init__(
        self, pool: list[Candidate], *, review_enabled: bool,
        after_review: Callable[[], None] | None = None,
    ) -> None:
        self.pool = pool
        self.review_enabled = review_enabled
        self.after_review = after_review
        self.reviewed_report: dict[str, Any] | None = None

    async def run_episode(self, **kwargs: Any) -> EpisodeResult:
        from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult

        if self.review_enabled:
            review = kwargs["evidence_review"]
            decision = await review.on_finish(pool=self.pool, queried_tables={}, budget=kwargs["budget"])
            assert not decision.continue_retrieval
            self.reviewed_report = review.report
            review.on_stop("finished")
            if self.after_review:
                self.after_review()
        else:
            assert "evidence_review" not in kwargs
        return EpisodeResult(
            pool=self.pool, stop_reason="finished", tokens_used=kwargs["budget"].tokens_used,
            model_name="contract-harness",
            steps=[AgentStep(
                step_index=0, tool_name="finish", tool_args={"notes": "100 tonnes"},
                observation_text="Executor says enough evidence", error=None,
                elapsed_ms=1, round_index=1,
            )],
        )


def _install_harness(monkeypatch: MonkeyPatch, harness: _FinishHarness) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.agent_explore.harness.resolve_harness",
        lambda **_kwargs: harness,
    )


def _context(*, review_evidence: bool, **changes: Any) -> RetrievalRouteContext:
    from shared.services.retrieval.execution.query_request import RetrievalQuery

    class RouteSession:
        async def rollback(self) -> None:
            pass

    return RetrievalQuery.from_parameters(**{
        "db": cast(AsyncSession, RouteSession()), "user_id": "reader", "namespace": "default",
        "query": "What is the payload to LEO?", "top_k": 1,
        "exclude_document_ids": [], "exclude_sections": [], "use_agentic": True,
        "review_evidence": review_evidence, **changes,
    }).build_route_context()


def _install_route_storage(
    monkeypatch: MonkeyPatch, rows: list[dict[str, Any]],
) -> list[str]:
    """Keep real assembly/composition; replace only database and trace I/O."""
    from shared.services.retrieval.execution import routes
    from shared.services.retrieval.execution.reference_resolver import ResolvedWorkflowReferences

    events: list[str] = []

    @asynccontextmanager
    async def fresh_session() -> AsyncIterator[AsyncSession]:
        events.append("open")
        try:
            yield cast(AsyncSession, None)
        finally:
            events.append("close")

    async def resolve(**_kwargs: Any) -> ResolvedWorkflowReferences:
        copied = copy.deepcopy(rows)
        return ResolvedWorkflowReferences(refs=copied, rows=copied)

    class NoopTrace:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def create_run(self) -> None:
            pass

        def record_decision_trace_step(self, _step: Any) -> None:
            pass

        async def complete(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(routes, "open_fresh_database_context", fresh_session)
    monkeypatch.setattr(routes, "resolve_workflow_references", resolve)
    monkeypatch.setattr("shared.services.retrieval.trace.TraceRecorder", NoopTrace)
    return events


def _supported_assessment(payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    item = payload["evidence"][0]
    return json.dumps({
        "status": "sufficient", "reason": "The selected source states the payload.",
        "coverage": [{
            "facet_id": "F1", "requirement": payload["original_query"],
            "status": "supported", "citations": [{
                "evidence_id": item["evidence_id"],
                "start_span": item["citation_spans"][0]["span_id"],
                "end_span": item["citation_spans"][-1]["span_id"],
            }],
        }],
    }), {"total_tokens": 17}


async def test_disabled_agent_route_never_constructs_review_or_calls_reviewer(
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services.retrieval.execution import routes

    def unexpected_review(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Review is opt-in")

    rows = [_row()]
    events = _install_route_storage(monkeypatch, rows)
    _install_harness(monkeypatch, _FinishHarness(_pool(rows), review_enabled=False))
    monkeypatch.setattr("shared.services.retrieval.agent_explore.evidence_review.EvidenceReviewSession", unexpected_review)
    monkeypatch.setattr("shared.services.retrieval.agent_explore.evidence_review._default_reviewer", unexpected_review)

    outcome = await routes._run_agent_explore_route(_context(review_evidence=False))

    assert events == ["open", "close"]
    assert "evidence_review" not in outcome.response
    assert "23 tonnes" in str(outcome.response["evidence"])
    assert outcome.response["answer_text"] == ""


async def test_changed_final_hydration_invalidates_a_successful_review(
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services.retrieval.execution import routes

    rows = [_row()]
    events = _install_route_storage(monkeypatch, rows)
    captured: list[dict[str, Any]] = []

    async def reviewer(messages: list[dict[str, Any]], _max_tokens: int) -> tuple[str, dict[str, int]]:
        # Review never holds the hydration transaction while awaiting a model.
        assert events == ["open", "close"]
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        return _supported_assessment(payload)

    harness = _FinishHarness(
        _pool(rows), review_enabled=True,
        after_review=lambda: rows[0].update(content="Payload: 64 tonnes to LEO."),
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr("shared.services.retrieval.agent_explore.evidence_review._default_reviewer", reviewer)

    outcome = await routes._run_agent_explore_route(_context(review_evidence=True))

    assert len(captured) == 1
    assert "".join(span["text"] for span in captured[0]["evidence"][0]["citation_spans"]) == "Payload: 23 tonnes to LEO."
    assert harness.reviewed_report is not None
    assert harness.reviewed_report["status"] == "sufficient"
    report = outcome.response["evidence_review"]
    assert report["status"] == "unverified"
    assert report["coverage"] == []
    assert report["evidence_fingerprint"] != harness.reviewed_report["evidence_fingerprint"]
    assert report["attempts"] == 1
    assert "64 tonnes" in str(outcome.response["evidence"])
    assert "23 tonnes" not in str(outcome.response["evidence"])
    assert events == ["open", "close", "open", "close"]


async def test_seeded_review_sees_only_selected_evidence_surviving_scope_and_filters(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    from tests.contract.test_retrieval_contract import (
        _seed_retrieval_chunk_for_existing_document,
        _seed_retrieval_document,
    )

    namespace = "contract-reviewed-evidence"
    captured: list[dict[str, Any]] = []

    async with developer_api_client_factory() as client:
        target = await _seed_retrieval_document(
            user_id="local-dev-user", namespace=namespace, source_file_name="launch.pdf",
            section_path="Launch/Capacity", content="Payload: 23 tonnes to LEO.",
            chunk_metadata={"summary": "Wrong summary: 100 tonnes.", "page_nums": [4]},
        )
        secret = await _seed_retrieval_chunk_for_existing_document(
            user_id="local-dev-user", namespace=namespace, document=target,
            section_path="Launch/Secret", content="Excluded section marker",
            chunk_id="excluded-section",
        )
        await _seed_retrieval_chunk_for_existing_document(
            user_id="local-dev-user", namespace=namespace, document=target,
            section_path="Launch/Filler", content="Unpicked scope filler",
            chunk_id="unpicked-filler",
        )
        other: dict[str, dict[str, str]] = {}
        for label, chunk_type, document_namespace in (
            ("wrong-type", "table", namespace),
            ("excluded-document", "text", namespace),
            ("outside-allowlist", "text", namespace),
            ("foreign-namespace", "text", "contract-reviewed-foreign"),
        ):
            other[label] = await _seed_retrieval_document(
                user_id="local-dev-user", namespace=document_namespace,
                source_file_name=f"{label}.pdf", section_path=f"Launch/{label}",
                content=f"Forbidden support: {label}", chunk_type=chunk_type,
            )
        rows = [
            {**item, "source_file_name": "launch.pdf"}
            for item in [target, secret, *other.values()]
        ]
        harness = _FinishHarness(_pool(rows), review_enabled=True)
        _install_harness(monkeypatch, harness)

        async def reviewer(messages: list[dict[str, Any]], _max_tokens: int) -> tuple[str, dict[str, int]]:
            payload = json.loads(messages[1]["content"])
            captured.append(payload)
            return _supported_assessment(payload)

        monkeypatch.setattr("shared.services.retrieval.agent_explore.evidence_review._default_reviewer", reviewer)
        response = await client.post("/api/v1/retrieval/query", json={
            "namespace": namespace, "query": "What is the payload to LEO?",
            "top_k": 1, "use_agentic": True, "review_evidence": True,
            "include_document_ids": [target["document_id"], other["wrong-type"]["document_id"], other["excluded-document"]["document_id"]],
            "exclude_document_ids": [other["excluded-document"]["document_id"]],
            "exclude_sections": [{"document_id": target["document_id"], "section_path": "Launch/Secret"}],
            "chunk_types": ["text"],
        })

    assert response.status_code == 200
    body = response.json()
    assert body["router_used"] == "agent_explore"
    assert len(captured) == 1
    assert len(captured[0]["evidence"]) == 1
    reviewed = captured[0]["evidence"][0]
    reviewed_text = "".join(span["text"] for span in reviewed["citation_spans"])
    assert reviewed_text == "Payload: 23 tonnes to LEO."
    assert reviewed["document_id"] == target["document_id"]
    assert reviewed["chunk_id"] == target["chunk_id"]
    assert reviewed["revision"] == target["job_result_id"]
    assert reviewed["page_nums"] == [4]
    assert len(body["results"]) == 1
    assert body["results"][0]["content"] == reviewed_text
    assert reviewed_text in str(body["evidence"])
    report = body["evidence_review"]
    assert report["status"] == "sufficient"
    assert report["attempts"] == 1 and report["reviewer_tokens"] == 17
    assert report["sources"] == [{key: value for key, value in reviewed.items() if key != "citation_spans"}]
    assert harness.reviewed_report is not None
    assert report["evidence_fingerprint"] == harness.reviewed_report["evidence_fingerprint"]
    assert body["answer_text"] == ""


async def test_review_cache_is_separate_and_unverified_attempts_are_retried(
    monkeypatch: MonkeyPatch,
) -> None:
    from shared.services.retrieval import app_service, cache_service
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
    from shared.services.retrieval.execution.route_types import RetrievalRouteOutcome

    class MemoryCache:
        def __init__(self) -> None:
            self.values: dict[str, Any] = {}

        async def get(self, key: str, default: Any = None) -> Any:
            return copy.deepcopy(self.values.get(key, default))

        async def set(self, key: str, value: Any, **_kwargs: Any) -> None:
            self.values[key] = copy.deepcopy(value)

    cache = MemoryCache()
    route_calls: list[bool] = []

    async def pins(*_args: Any, **_kwargs: Any) -> RetrievalRevisionPins:
        return RetrievalRevisionPins({"doc_launch": "revision_launch"}, generation=1)

    async def stable(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def route(context: RetrievalRouteContext) -> RetrievalRouteOutcome:
        route_calls.append(context.review_evidence)
        response: dict[str, Any] = {
            "namespace": context.namespace, "query": context.query,
            "router_used": "agent_explore", "results": [],
        }
        if context.review_evidence:
            response["evidence_review"] = _report("unverified" if route_calls.count(True) == 1 else "sufficient")
        return RetrievalRouteOutcome(response, [], "retrieval", 0, "results")

    monkeypatch.setattr(cache_service.RedisServiceFactory, "get_service", lambda: cache)
    monkeypatch.setattr(plan, "capture_revision_pins", pins)
    monkeypatch.setattr(plan, "is_revision_generation_stable", stable)
    monkeypatch.setattr(plan, "run_retrieval_route", route)
    monkeypatch.setattr(plan, "schedule_retrieval_hit_stats_update", lambda **_kwargs: None)
    kwargs: dict[str, Any] = dict(
        db=cast(AsyncSession, None), user_id="reader", namespace="default", query="payload",
        top_k=1, exclude_document_ids=[], exclude_sections=[], use_agentic=True,
    )

    normal = await app_service.run_retrieval_query(**kwargs)
    first_review = await app_service.run_retrieval_query(**kwargs, review_evidence=True)
    assert first_review["evidence_review"]["status"] == "unverified"
    assert len(cache.values) == 1
    second_review = await app_service.run_retrieval_query(**kwargs, review_evidence=True)
    cached_review = await app_service.run_retrieval_query(**kwargs, review_evidence=True)
    cached_normal = await app_service.run_retrieval_query(**kwargs)

    assert route_calls == [False, True, True]
    assert len(cache.values) == 2
    assert "evidence_review" not in normal and cached_normal == normal
    assert second_review["evidence_review"]["status"] == "sufficient"
    assert cached_review == second_review


@pytest.mark.parametrize("route_name", ["small_corpus_all", "classic_topk"])
async def test_routes_without_a_reviewer_report_unverified(
    monkeypatch: MonkeyPatch, route_name: str,
) -> None:
    from shared.services.retrieval.execution import routes
    from shared.services.retrieval.execution.route_types import RetrievalRouteOutcome

    response = {"router_used": route_name, "evidence": [], "answer_text": ""}
    outcome = RetrievalRouteOutcome(response, [], "retrieval", 0, "results")

    async def small(_context: RetrievalRouteContext) -> RetrievalRouteOutcome | None:
        return outcome if route_name == "small_corpus_all" else None

    async def classic(_context: RetrievalRouteContext) -> RetrievalRouteOutcome:
        return outcome

    def unexpected_reviewer(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("This route has no agent review")

    monkeypatch.setattr(routes, "_try_run_small_corpus_route", small)
    monkeypatch.setattr(routes, "_run_classic_topk_route", classic)
    monkeypatch.setattr("shared.services.retrieval.agent_explore.evidence_review._default_reviewer", unexpected_reviewer)

    actual = await routes.run_retrieval_route(_context(review_evidence=True, use_agentic=False))

    assert actual.response["router_used"] == route_name
    assert actual.response["evidence_review"]["status"] == "unverified"
    assert actual.response["evidence_review"]["attempts"] == 0
    assert actual.response["answer_text"] == ""
