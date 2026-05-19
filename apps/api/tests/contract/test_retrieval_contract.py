from collections.abc import Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.contract_database import ContractDatabase
from shared.services.retrieval.agentic.core.types import AgenticResult
from shared.services.retrieval.workflow.run_request import WorkflowRunRequest
from shared.services.retrieval.workflow.types import PlannedStep, QueryPlan, WorkflowResult

LLMFnInput = str | Sequence[dict[str, Any]]
LLMFn = Callable[[LLMFnInput], Coroutine[Any, Any, str]]


async def _seed_retrieval_document(
    *,
    user_id: str,
    namespace: str,
    source_file_name: str,
    section_path: str,
    content: str,
    chunk_id: str | None = None,
    chunk_type: str = "text",
    file_path: str | None = None,
    chunk_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    document_id = f"doc_{uuid4().hex[:12]}"
    job_id = f"job_{uuid4().hex[:12]}"
    job_result_id = str(uuid4())
    section_id = f"sec_{uuid4().hex[:12]}"
    resolved_chunk_id = chunk_id or f"chunk_{uuid4().hex[:12]}"

    await ContractDatabase.insert_job(
        job_id=job_id,
        user_id=user_id,
        status="done",
        source_type="file",
        job_metadata={
            "document_id": document_id,
            "namespace": namespace,
            "source_type": "file",
        },
    )
    await ContractDatabase.insert_document(
        document_id=document_id,
        user_id=user_id,
        namespace=namespace,
        source_file_name=source_file_name,
    )
    await ContractDatabase.insert_job_result(
        job_result_id=job_result_id,
        job_id=job_id,
        document_id=document_id,
        delivery_mode="inline",
    )
    await ContractDatabase.execute(
        """
        UPDATE documents
        SET current_job_result_id = :job_result_id
        WHERE document_id = :document_id
        """,
        {
            "job_result_id": job_result_id,
            "document_id": document_id,
        },
    )
    await ContractDatabase.insert_document_section(
        section_id=section_id,
        user_id=user_id,
        namespace=namespace,
        document_id=document_id,
        job_result_id=job_result_id,
        section_path=section_path,
        section_title=section_path.split("/")[-1],
    )
    await ContractDatabase.insert_document_chunk(
        chunk_id=resolved_chunk_id,
        user_id=user_id,
        namespace=namespace,
        document_id=document_id,
        job_result_id=job_result_id,
        section_id=section_id,
        chunk_type=chunk_type,
        content=content,
        section_path=section_path,
        file_path=file_path,
        chunk_metadata=chunk_metadata,
    )

    return {
        "document_id": document_id,
        "job_id": job_id,
        "job_result_id": job_result_id,
        "section_id": section_id,
        "chunk_id": resolved_chunk_id,
        "section_path": section_path,
    }


async def _seed_retrieval_chunk_for_existing_document(
    *,
    user_id: str,
    namespace: str,
    document: dict[str, str],
    section_path: str,
    content: str,
    chunk_id: str,
) -> dict[str, str]:
    section_id = f"sec_{uuid4().hex[:12]}"

    await ContractDatabase.insert_document_section(
        section_id=section_id,
        user_id=user_id,
        namespace=namespace,
        document_id=document["document_id"],
        job_result_id=document["job_result_id"],
        section_path=section_path,
        section_title=section_path.split("/")[-1],
    )
    await ContractDatabase.insert_document_chunk(
        chunk_id=chunk_id,
        user_id=user_id,
        namespace=namespace,
        document_id=document["document_id"],
        job_result_id=document["job_result_id"],
        section_id=section_id,
        chunk_type="text",
        content=content,
        section_path=section_path,
    )

    return {
        "document_id": document["document_id"],
        "job_id": document["job_id"],
        "job_result_id": document["job_result_id"],
        "section_id": section_id,
        "chunk_id": chunk_id,
        "section_path": section_path,
    }


def _result_source(result: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], result["source"])

@pytest.mark.asyncio
async def test_agentic_workflow_should_pass_full_request_policy_to_step_adapter(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    captured_requests: list[dict[str, object]] = []

    async def fake_plan(
        self: object,
        *,
        query: str,
        corpus_total_docs: int = 0,
        corpus_total_chunks: int = 0,
    ) -> QueryPlan:
        return QueryPlan(
            original_query=query,
            steps=[PlannedStep(id="request-policy", sub_query="policy marker")],
            final_strategy="concat_final_parts",
            reasoning_summary=(
                f"request policy contract for {corpus_total_docs} docs "
                f"and {corpus_total_chunks} chunks"
            ),
        )

    async def fake_retrieval_run(
        self: object,
        db: object,
        **kwargs: object,
    ) -> AgenticResult:
        del self, db
        captured_requests.append(kwargs)
        return AgenticResult(
            evidence_text="policy evidence",
            answer_text="policy answer",
            referenced_chunks=[
                {
                    "chunk_id": policy_document["chunk_id"],
                    "document_id": policy_document["document_id"],
                    "chunk_type": "text",
                    "section_path": policy_document["section_path"],
                    "file_path": "",
                    "job_id": policy_document["job_id"],
                }
            ],
            router_used="contract_fake_agent",
        )

    async with developer_api_client_factory() as api_client:
        policy_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-agentic-request-policy",
            source_file_name="policy.pdf",
            section_path="Root/Policy",
            content="policy marker content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-agentic-request-policy",
            source_file_name="filler.pdf",
            section_path="Root/Filler",
            content="filler content",
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.planner.QueryPlanner.plan",
            fake_plan,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.step_runner.RetrievalAgent.run",
            fake_retrieval_run,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-agentic-request-policy",
                "query": "policy marker",
                "top_k": 1,
                "data_type": 2,
                "signal_paths": ["Root"],
                "filter_mode": "keep",
                "channels": ["content"],
                "channel_weights": {"content": 2.0},
                "internal_recall_k": 23,
                "threshold": 0.4,
                "rerank": True,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["user_id"] == "local-dev-user"
    assert request["namespace"] == "contract-agentic-request-policy"
    assert request["query"] == "policy marker"
    assert request["top_k"] == 1
    assert request["exclude_document_ids"] == []
    assert request["exclude_sections"] == []
    assert request["data_type"] == 2
    assert request["signal_paths"] == ["Root"]
    assert request["filter_mode"] == "keep"
    assert request["channels"] == ["content"]
    assert request["channel_weights"] == {"content": 2.0}
    assert request["internal_recall_k"] == 23


@pytest.mark.asyncio
async def test_should_return_seeded_retrieval_results_for_the_authenticated_user(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        seeded_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-retrieval",
            source_file_name="contract-retrieval.pdf",
            section_path="contract/intro",
            content="alpha contract retrieval content",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-retrieval",
                "query": "alpha",
                "top_k": 10,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["namespace"] == "contract-retrieval"
    assert response_json["query"] == "alpha"
    assert response_json["router_used"] == "small_corpus_all"
    assert len(results) == 1
    assert results[0]["chunk_type"] == "text"
    assert results[0]["content"] == "alpha contract retrieval content"
    assert results[0]["score"] == 1.0
    assert results[0]["source"] == {
        "document_id": seeded_document["document_id"],
        "source_file_name": "contract-retrieval.pdf",
        "section_path": "contract/intro",
    }


@pytest.mark.asyncio
async def test_should_default_the_namespace_to_default_when_it_is_omitted(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        seeded_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="default",
            source_file_name="default-retrieval.pdf",
            section_path="default/overview",
            content="default namespace retrieval text",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "query": "default namespace",
                "top_k": 10,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["namespace"] == "default"
    assert len(results) == 1
    assert _result_source(results[0])["document_id"] == seeded_document["document_id"]


@pytest.mark.asyncio
async def test_should_return_empty_results_for_an_empty_query(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={"namespace": "default", "query": "   "},
        )

    assert response.status_code == 200
    assert response.json() == {
        "namespace": "default",
        "query": "",
        "router_used": "empty_query_filtered",
        "results": [],
        "answer_text": None,
        "referenced_chunks": [],
    }


@pytest.mark.asyncio
async def test_legacy_retrieval_should_rank_hot_chunk_before_cold_chunk_when_discovery_scores_tie(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        cold_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-hot-ranking",
            source_file_name="cold.pdf",
            section_path="ranking/cold",
            content="same ranking marker cold",
        )
        hot_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-hot-ranking",
            source_file_name="hot.pdf",
            section_path="ranking/hot",
            content="same ranking marker hot",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-hot-ranking",
            source_file_name="filler.pdf",
            section_path="ranking/filler",
            content="same ranking marker filler",
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await ContractDatabase.execute(
            """
            INSERT INTO retrieval_hit_stats (
                id,
                user_id,
                namespace,
                hit_kind,
                document_id,
                chunk_id,
                hit_count,
                last_hit_at,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :user_id,
                :namespace,
                'chunk',
                :document_id,
                :chunk_id,
                :hit_count,
                :now,
                :now,
                :now
            )
            """,
            {
                "id": f"rhs_{uuid4().hex[:12]}",
                "user_id": "local-dev-user",
                "namespace": "contract-hot-ranking",
                "document_id": hot_document["document_id"],
                "chunk_id": hot_document["chunk_id"],
                "hit_count": 100,
                "now": now,
            },
        )

        def to_channel_row(document: dict[str, str]) -> dict[str, object]:
            return {
                "document_id": document["document_id"],
                "chunk_id": document["chunk_id"],
                "section_id": document["section_id"],
                "section_path": document["section_path"],
                "source_file_name": "cold.pdf"
                if document["document_id"] == cold_document["document_id"]
                else "hot.pdf",
                "chunk_type": "text",
                "content": "same ranking marker",
                "score": 1.0,
                "file_path": None,
                "chunk_metadata": {},
                "job_result_id": document["job_result_id"],
                "job_id": document["job_id"],
                "sort_order": 0,
            }

        async def fake_content_channel(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [
                to_channel_row(hot_document),
                to_channel_row(cold_document),
            ]

        async def fake_path_channel(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [
                to_channel_row(cold_document),
                to_channel_row(hot_document),
            ]

        async def fake_graph_routing(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

        monkeypatch.setattr(
            "shared.services.retrieval.execution.legacy_route.path_channel",
            fake_path_channel,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.execution.legacy_route.content_channel",
            fake_content_channel,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.execution.legacy_route.list_graph_routed_chunks",
            fake_graph_routing,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-hot-ranking",
                "query": "same ranking marker",
                "top_k": 1,
                "channels": ["path", "content"],
                "channel_weights": {"path": 1.0, "content": 1.0},
                "use_agentic": False,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], response_json["results"])

    assert len(results) == 1
    assert _result_source(results[0])["document_id"] == hot_document["document_id"]


@pytest.mark.asyncio
async def test_agentic_retrieval_should_reference_root_only_document_content(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK_ENABLED", "true")

    async with developer_api_client_factory() as api_client:
        rooted_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-root-retrieval",
            source_file_name="root-only.pdf",
            section_path="Root",
            content="root only diluted earnings marker content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-root-retrieval",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="unrelated filler content",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-root-retrieval",
                "query": "diluted earnings marker",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["router_used"] == "workflow_single_step"
    assert {
        "chunk_id": rooted_document["chunk_id"],
        "document_id": rooted_document["document_id"],
        "chunk_type": "text",
        "section_path": "root-only.pdf",
        "file_path": None,
        "job_id": rooted_document["job_id"],
    } in referenced_chunks
    assert results[0]["content"] == "root only diluted earnings marker content"
    assert results[0]["source"] == {
        "document_id": rooted_document["document_id"],
        "source_file_name": "root-only.pdf",
        "section_path": "Root",
    }


@pytest.mark.asyncio
async def test_agentic_retrieval_should_reference_discovery_content_when_navigation_selects_nothing(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK_ENABLED", "true")

    async with developer_api_client_factory() as api_client:
        discovered_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-discovery-fallback",
            source_file_name="discovery.pdf",
            section_path="Findings",
            content="discovery fallback EBITDA margin marker content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-discovery-fallback",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="unrelated filler content",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-discovery-fallback",
                "query": "EBITDA margin marker",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["router_used"] == "workflow_single_step"
    assert {
        "chunk_id": discovered_document["chunk_id"],
        "document_id": discovered_document["document_id"],
        "chunk_type": "text",
        "section_path": discovered_document["section_path"],
        "file_path": None,
        "job_id": discovered_document["job_id"],
    } in referenced_chunks
    assert results[0]["content"] == "discovery fallback EBITDA margin marker content"
    assert results[0]["source"] == {
        "document_id": discovered_document["document_id"],
        "source_file_name": "discovery.pdf",
        "section_path": discovered_document["section_path"],
    }


@pytest.mark.asyncio
async def test_agentic_retrieval_should_not_send_table_artifacts_to_vlm(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK_ENABLED", "true")
    vlm_calls: list[LLMFnInput] = []

    async def fake_vlm(prompt: LLMFnInput) -> str:
        vlm_calls.append(prompt)
        return '{"status":"DONE","answer":"unexpected table VLM answer"}'

    def fake_create_retrieval_vlm_fn(**_kwargs: object) -> LLMFn:
        return fake_vlm

    class FakeResultStorage:
        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str | None:
            del expires_in
            return f"https://assets.example.com/{job_id}/{artifact_ref}"

        def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
            if not artifact_ref:
                return None
            normalized = artifact_ref.strip().replace("\\", "/").lstrip("/")
            if not normalized:
                return None
            root_dir = normalized.split("/", 1)[0]
            if root_dir not in {"images", "tables"}:
                return None
            return normalized

    def fake_get_result_storage() -> FakeResultStorage:
        return FakeResultStorage()

    async with developer_api_client_factory() as api_client:
        monkeypatch.setattr(
            "shared.services.retrieval.llm_adapter.create_retrieval_vlm_fn",
            fake_create_retrieval_vlm_fn,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.hydration.assets.get_result_storage",
            fake_get_result_storage,
        )
        table_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-agentic-table-vlm-filter",
            source_file_name="table-report.md",
            section_path="Realdata Results Summary / Main Metrics",
            content=(
                "<table><tr><th>budget</th><th>metric</th><th>value</th></tr>"
                "<tr><td>1000</td><td>Flat inspect_evidence_score_mean</td>"
                "<td>0.5674</td></tr></table>"
            ),
            chunk_type="table",
            file_path="tables/table-0-main-metrics.html",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-agentic-table-vlm-filter",
            source_file_name="filler-table-report.md",
            section_path="Appendix / Filler Metrics",
            content=(
                "<table><tr><th>metric</th><th>value</th></tr>"
                "<tr><td>unrelated filler metric</td><td>999</td></tr></table>"
            ),
            chunk_type="table",
            file_path="tables/table-1-filler-metrics.html",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-agentic-table-vlm-filter",
                "query": "budget 1000 Flat inspect_evidence_score_mean",
                "top_k": 1,
                "data_type": 4,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["router_used"] == "workflow_single_step"
    assert vlm_calls == []
    matching_references = [
        reference
        for reference in referenced_chunks
        if reference["chunk_id"] == table_document["chunk_id"]
    ]
    assert len(matching_references) == 1
    assert matching_references[0]["document_id"] == table_document["document_id"]
    assert matching_references[0]["chunk_type"] == "table"
    assert matching_references[0]["section_path"] == table_document["section_path"]
    assert matching_references[0]["file_path"] == "tables/table-0-main-metrics.html"
    assert matching_references[0]["job_id"] == table_document["job_id"]
    assert str(matching_references[0]["asset_url"]).startswith(
        "https://assets.example.com/"
    )
    assert len(results) == 1
    assert results[0]["chunk_type"] == "table"
    assert _result_source(results[0])["document_id"] == table_document["document_id"]


@pytest.mark.asyncio
async def test_agentic_retrieval_should_not_hydrate_references_outside_request_scope(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeWorkflowOrchestrator:
        async def run_request(
            self,
            _db: AsyncSession,
            *,
            request: WorkflowRunRequest,
        ) -> WorkflowResult:
            return WorkflowResult(
                namespace=request.namespace,
                query=request.query,
                router_used="workflow_single_step",
                answer_text="foreign reference answer",
                referenced_chunks=[
                    {
                        "chunk_id": foreign_document["chunk_id"],
                        "document_id": foreign_document["document_id"],
                        "chunk_type": "text",
                        "section_path": foreign_document["section_path"],
                        "file_path": None,
                        "job_id": foreign_document["job_id"],
                    }
                ],
            )

    async with developer_api_client_factory() as api_client:
        request_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-visible-scope",
            source_file_name="visible.pdf",
            section_path="visible/section",
            content="visible scoped content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-visible-scope",
            source_file_name="visible-filler.pdf",
            section_path="visible/filler",
            content="visible scoped filler content",
        )
        foreign_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-foreign-scope",
            source_file_name="foreign.pdf",
            section_path="foreign/section",
            content="foreign scoped content should not leak",
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.orchestrator.WorkflowOrchestrator",
            FakeWorkflowOrchestrator,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-visible-scope",
                "query": "visible",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert request_document["document_id"] != foreign_document["document_id"]
    assert referenced_chunks == []
    assert results == []


@pytest.mark.asyncio
async def test_agentic_retrieval_should_drop_references_that_do_not_match_the_hydrated_section(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeWorkflowOrchestrator:
        async def run_request(
            self,
            _db: AsyncSession,
            *,
            request: WorkflowRunRequest,
        ) -> WorkflowResult:
            return WorkflowResult(
                namespace=request.namespace,
                query=request.query,
                router_used="workflow_single_step",
                answer_text="mismatched section answer",
                referenced_chunks=[
                    {
                        "chunk_id": visible_chunk["chunk_id"],
                        "document_id": visible_chunk["document_id"],
                        "chunk_type": "text",
                        "section_path": "wrong/section",
                        "file_path": None,
                        "job_id": visible_chunk["job_id"],
                    }
                ],
            )

    async with developer_api_client_factory() as api_client:
        visible_chunk = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-reference-section-match",
            source_file_name="visible.pdf",
            section_path="right/section",
            content="visible scoped content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-reference-section-match",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="filler content",
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.orchestrator.WorkflowOrchestrator",
            FakeWorkflowOrchestrator,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-reference-section-match",
                "query": "visible",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert referenced_chunks == []
    assert results == []


@pytest.mark.asyncio
async def test_agentic_workflow_should_preserve_references_with_the_same_chunk_id_across_documents(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    shared_chunk_id = f"chunk_{uuid4().hex[:12]}"

    async def fake_plan(
        self: object,
        *,
        query: str,
        corpus_total_docs: int = 0,
        corpus_total_chunks: int = 0,
    ) -> QueryPlan:
        return QueryPlan(
            original_query=query,
            steps=[
                PlannedStep(id="first", sub_query="first shared reference"),
                PlannedStep(id="second", sub_query="second shared reference"),
            ],
            final_strategy="concat_final_parts",
            reasoning_summary=(
                f"forced two-step contract plan for {corpus_total_docs} docs "
                f"and {corpus_total_chunks} chunks"
            ),
        )

    async def fake_retrieval_run(
        self: object,
        db: object,
        **kwargs: object,
    ) -> AgenticResult:
        query = str(kwargs["query"])
        document = first_document if query == "first shared reference" else second_document
        return AgenticResult(
            evidence_text=f"evidence for {document['document_id']}",
            answer_text=f"answer for {document['document_id']}",
            referenced_chunks=[
                {
                    "chunk_id": shared_chunk_id,
                    "document_id": document["document_id"],
                    "chunk_type": "text",
                    "section_path": document["section_path"],
                    "file_path": "",
                    "job_id": document["job_id"],
                }
            ],
            router_used="contract_fake_agent",
        )

    async with developer_api_client_factory() as api_client:
        first_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-shared-chunk-id",
            source_file_name="first.pdf",
            section_path="first/section",
            content="shared deterministic content",
            chunk_id=shared_chunk_id,
        )
        second_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-shared-chunk-id",
            source_file_name="second.pdf",
            section_path="second/section",
            content="shared deterministic content",
            chunk_id=shared_chunk_id,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.planner.QueryPlanner.plan",
            fake_plan,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.step_runner.RetrievalAgent.run",
            fake_retrieval_run,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-shared-chunk-id",
                "query": "show both shared references",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    referenced_document_ids = {
        cast(str, reference["document_id"]) for reference in referenced_chunks
    }
    result_document_ids = {
        cast(str, _result_source(result)["document_id"]) for result in results
    }

    assert referenced_document_ids == {
        first_document["document_id"],
        second_document["document_id"],
    }
    assert result_document_ids == {
        first_document["document_id"],
        second_document["document_id"],
    }


@pytest.mark.asyncio
async def test_agentic_workflow_should_preserve_references_with_the_same_chunk_id_across_sections(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    shared_chunk_id = f"chunk_{uuid4().hex[:12]}"

    async def fake_plan(
        self: object,
        *,
        query: str,
        corpus_total_docs: int = 0,
        corpus_total_chunks: int = 0,
    ) -> QueryPlan:
        return QueryPlan(
            original_query=query,
            steps=[
                PlannedStep(id="first", sub_query="first shared section"),
                PlannedStep(id="second", sub_query="second shared section"),
            ],
            final_strategy="concat_final_parts",
            reasoning_summary=(
                f"forced section identity contract plan for {corpus_total_docs} docs "
                f"and {corpus_total_chunks} chunks"
            ),
        )

    async def fake_retrieval_run(
        self: object,
        db: object,
        **kwargs: object,
    ) -> AgenticResult:
        query = str(kwargs["query"])
        chunk = first_chunk if query == "first shared section" else second_chunk
        return AgenticResult(
            evidence_text=f"evidence for {chunk['section_path']}",
            answer_text=f"answer for {chunk['section_path']}",
            referenced_chunks=[
                {
                    "chunk_id": shared_chunk_id,
                    "document_id": chunk["document_id"],
                    "chunk_type": "text",
                    "section_path": chunk["section_path"],
                    "file_path": "",
                    "job_id": chunk["job_id"],
                }
            ],
            router_used="contract_fake_agent",
        )

    async with developer_api_client_factory() as api_client:
        first_chunk = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-shared-section-chunk-id",
            source_file_name="same-document.pdf",
            section_path="first/section",
            content="repeated deterministic content",
            chunk_id=shared_chunk_id,
        )
        second_chunk = await _seed_retrieval_chunk_for_existing_document(
            user_id="local-dev-user",
            namespace="contract-shared-section-chunk-id",
            document=first_chunk,
            section_path="second/section",
            content="repeated deterministic content",
            chunk_id=shared_chunk_id,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.planner.QueryPlanner.plan",
            fake_plan,
        )
        monkeypatch.setattr(
            "shared.services.retrieval.workflow.step_runner.RetrievalAgent.run",
            fake_retrieval_run,
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-shared-section-chunk-id",
                "query": "show both shared section references",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    referenced_section_paths = {
        cast(str, reference["section_path"]) for reference in referenced_chunks
    }
    result_section_paths = {
        cast(str, _result_source(result)["section_path"]) for result in results
    }

    assert referenced_section_paths == {
        first_chunk["section_path"],
        second_chunk["section_path"],
    }
    assert result_section_paths == {
        first_chunk["section_path"],
        second_chunk["section_path"],
    }


@pytest.mark.asyncio
async def test_should_return_request_validation_failure_for_an_invalid_channel(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "default",
                "query": "alpha",
                "channels": ["invalid-channel"],
            },
        )

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"] == "Request validation failed"
    assert violations[0]["field"] == "body.channels"
    assert "Invalid channel" in cast(str, violations[0]["description"])


@pytest.mark.asyncio
async def test_should_exclude_matching_document_ids_from_the_response(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        included_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-retrieval",
            source_file_name="included.pdf",
            section_path="contract/included",
            content="retrieval included content",
        )
        excluded_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-retrieval",
            source_file_name="excluded.pdf",
            section_path="contract/excluded",
            content="retrieval excluded content",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-retrieval",
                "query": "retrieval",
                "exclude_document_ids": [excluded_document["document_id"]],
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], response_json["results"])

    assert len(results) == 1
    assert _result_source(results[0])["document_id"] == included_document["document_id"]


@pytest.mark.asyncio
async def test_should_exclude_matching_sections_from_the_response(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        included_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-retrieval",
            source_file_name="included-section.pdf",
            section_path="contract/keep",
            content="section keep content",
        )
        excluded_document = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-retrieval",
            source_file_name="excluded-section.pdf",
            section_path="contract/exclude",
            content="section exclude content",
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-retrieval",
                "query": "section",
                "exclude_sections": [
                    {
                        "document_id": excluded_document["document_id"],
                        "section_path": excluded_document["section_path"],
                    }
                ],
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], response_json["results"])

    assert len(results) == 1
    assert _result_source(results[0])["document_id"] == included_document["document_id"]
    assert _result_source(results[0])["section_path"] == included_document["section_path"]
