"""Only explicit retrieval records hits, on a fresh run and on a cache hit."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-kw04-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.core.config import settings
from shared.services.retrieval.execution import plan
from shared.services.retrieval.execution.query_request import RetrievalQuery
from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
from shared.services.retrieval.execution.route_types import RetrievalRouteOutcome

HIT: dict[str, Any] = {"source": {"document_id": "doc", "chunk_id": "chunk"}}


def recordScheduledHits(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    scheduled: list[list[dict[str, Any]]] = []

    def schedule(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
        scheduled.append(results)

    monkeypatch.setattr(plan, "schedule_retrieval_hit_stats_update", schedule)

    async def project(response: dict[str, Any]) -> dict[str, Any]:
        return response

    monkeypatch.setattr(plan, "project_public_retrieval_response", project)
    return scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize(("purpose", "isRecorded"), [("explicit", True), ("automatic", False)])
async def test_fresh_retrieval_records_hits_only_when_explicit(
    monkeypatch: pytest.MonkeyPatch, purpose: str, isRecorded: bool
) -> None:
    scheduled = recordScheduledHits(monkeypatch)

    async def epoch(request: RetrievalQuery) -> int:
        return 1

    async def pins(*arguments: object, **keywords: object) -> RetrievalRevisionPins:
        return RetrievalRevisionPins({"doc": "rev"})

    async def missCache(**arguments: object) -> tuple[None, None]:
        return None, None

    async def route(context: object) -> RetrievalRouteOutcome:
        return RetrievalRouteOutcome(
            response={"router_used": "classic_topk", "results": [HIT]},
            hit_stats_results=[HIT],
            completion_label="classic",
            completion_count=1,
            completion_detail="",
        )

    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
    monkeypatch.setattr(plan.RetrievalExecutionPlan, "_capture_postgresql_epoch", staticmethod(epoch))
    monkeypatch.setattr(plan, "capture_revision_pins", pins)
    monkeypatch.setattr(plan, "_read_cached_response", missCache)
    monkeypatch.setattr(plan, "run_retrieval_route", route)
    request: RetrievalQuery = RetrievalQuery.from_parameters(
        db=SimpleNamespace(in_transaction=lambda: True),
        user_id="user",
        namespace="ns",
        query="query",
        top_k=1,
        exclude_document_ids=[],
        exclude_sections=[],
        use_agentic=False,
        purpose=purpose,
    )

    response = await plan.RetrievalExecutionPlan(request).execute()

    assert response["results"] == [HIT]
    assert scheduled == ([[HIT]] if isRecorded else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("isRecorded", [True, False])
async def test_cache_hit_records_hits_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, isRecorded: bool
) -> None:
    scheduled = recordScheduledHits(monkeypatch)

    async def cached(**arguments: object) -> tuple[int, dict[str, Any]]:
        return 3, {"results": [HIT]}

    monkeypatch.setattr(plan, "get_cached_retrieval_query_result", cached)

    version, response = await plan._read_cached_response(
        user_id="user",
        namespace="ns",
        query="query",
        top_k=1,
        exclude_document_ids=[],
        exclude_sections=[],
        cache_extra={},
        records_hits=isRecorded,
    )

    assert version == 3
    assert response == {"results": [HIT]}
    assert scheduled == ([[HIT]] if isRecorded else [])


def test_purpose_defaults_to_explicit_and_stays_out_of_the_cache_key() -> None:
    def build(**keywords: Any) -> RetrievalQuery:
        return RetrievalQuery.from_parameters(
            db=SimpleNamespace(),
            user_id="user",
            namespace="ns",
            query="query",
            top_k=1,
            exclude_document_ids=[],
            exclude_sections=[],
            **keywords,
        )

    assert build().records_hits
    assert not build(purpose="automatic").records_hits
    assert build().build_cache_extra() == build(purpose="automatic").build_cache_extra()
