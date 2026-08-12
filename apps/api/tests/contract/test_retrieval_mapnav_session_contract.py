"""Map-nav route session lifetime: route rollback before fresh final hydration."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.execution import routes as route_module
from shared.services.retrieval.execution.reference_resolver import (
    ResolvedWorkflowReferences,
)
from shared.services.retrieval.execution.route_types import RetrievalRouteContext
from shared.services.retrieval.nav._compat import AgentStep, Chunk, EpisodeResult
from shared.services.retrieval.nav.nav_knowhere import SectionRow, UnitRow
from shared.services.retrieval.nav_snapshot import build_nav_snapshot

RouteRow = dict[str, object]


class _RecordingRouteSession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def rollback(self) -> None:
        self._events.append("route_rollback")


@pytest.mark.asyncio
async def test_mapnav_route_should_release_route_session_before_fresh_final_hydration(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    route_db = _RecordingRouteSession(events)
    fresh_db = object()

    snap = build_nav_snapshot(
        document_titles={"doc_contract": "Contract"},
        sections_by_doc={
            "doc_contract": [
                SectionRow(
                    section_id="sec_contract",
                    parent_section_id=None,
                    section_path="contract/section",
                    section_title="section",
                    section_level=0,
                    summary="",
                    sort_order=0,
                )
            ]
        },
        units_by_doc={
            "doc_contract": [
                UnitRow(
                    chunk_id="chunk_contract",
                    section_id="sec_contract",
                    chunk_type="text",
                    content="contract content",
                    sort_order=0,
                )
            ]
        },
        chunk_ref_index={
            "chunk_contract": {
                "document_id": "doc_contract",
                "section_path": "contract/section",
                "chunk_type": "text",
                "file_path": None,
                "job_id": "job_contract",
            }
        },
    )

    @asynccontextmanager
    async def fake_get_db_context() -> AsyncGenerator[AsyncSession, None]:
        events.append("fresh_db_open")
        try:
            yield cast(AsyncSession, fresh_db)
        finally:
            events.append("fresh_db_close")

    async def fake_load_nav_snapshot(
        db: AsyncSession,
        **_kwargs: Any,
    ) -> Any:
        assert db is route_db
        events.append("snapshot_loaded")
        return snap

    def fake_run_nav_episode(*_args: Any, **_kwargs: Any) -> EpisodeResult:
        events.append("nav_episode")
        assert events[:3] == [
            "snapshot_loaded",
            "route_rollback",
            "nav_episode",
        ]
        chunk = Chunk(
            node_id="chunk_contract",
            doc_id="doc_contract",
            text="contract content",
            line_ids=(0,),
            section_id="sec_contract",
        )
        return EpisodeResult(
            representation="mapnav",
            steps=[
                AgentStep(
                    step_idx=1,
                    action="query_plan",
                    detail={
                        "plan": {"subgoals": [], "coverage_checklist": []},
                        "token_limit": 100000,
                        "tokens_used_total": 1,
                        "tokens_used_delta": 1,
                        "elapsed_ms": 1,
                    },
                )
            ],
            scored_chunks=[(chunk, 1.0)],
            kept_chunks=[chunk],
            evidence_text="contract content",
            evidence_chars_actual=len("contract content"),
            retrieved_nodes=["chunk_contract"],
            stop_reason="completed",
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
        assert refs
        assert score_by_chunk_id is not None
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
        "shared.services.retrieval.nav_snapshot.load_nav_snapshot",
        fake_load_nav_snapshot,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.nav.run_nav_episode",
        fake_run_nav_episode,
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

    outcome = await route_module._run_mapnav_route(
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
        "snapshot_loaded",
        "route_rollback",
        "nav_episode",
        "fresh_db_open",
        "resolve_references",
        "assemble_results",
        "fresh_db_close",
    ]
    assert outcome.response["router_used"] == "mapnav"
    assert outcome.response["results"][0]["citation"]["document_id"] == "doc_contract"
    assert outcome.completion_label == "MAPNAV RETRIEVAL"
