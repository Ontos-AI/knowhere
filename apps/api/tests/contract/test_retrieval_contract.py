from collections.abc import Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase

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
    response_json = response.json()
    assert response_json["namespace"] == "default"
    assert response_json["query"] == ""
    assert response_json["router_used"] == "empty_query_filtered"
    assert response_json["evidence_text"] == ""
    assert response_json["answer_text"] == ""
    assert response_json["results"] == []
    assert response_json["referenced_chunks"] == []


async def test_retrieval_should_use_classic_topk_when_agentic_is_false(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    from tests.contract.test_retrieval_classic_map_unit_contract import (
        _publish_document,
    )

    async with developer_api_client_factory() as api_client:
        await _publish_document(
            namespace="contract-agentic-only",
            source_file_name="a.pdf",
            chunks=[
                {
                    "chunk_id": "classic-a",
                    "type": "text",
                    "content": "same ranking marker a",
                    "path": "a.pdf/Root/agentic/a",
                    "order": 1,
                    "metadata": {},
                }
            ],
        )
        await _publish_document(
            namespace="contract-agentic-only",
            source_file_name="b.pdf",
            chunks=[
                {
                    "chunk_id": "classic-b",
                    "type": "text",
                    "content": "same ranking marker b",
                    "path": "b.pdf/Root/agentic/b",
                    "order": 1,
                    "metadata": {},
                }
            ],
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-agentic-only",
                "query": "same ranking marker",
                "top_k": 1,
                "use_agentic": False,
            },
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    assert response_json["router_used"] == "classic_topk"


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


async def test_should_reject_legacy_channel_controls(
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
                "channels": ["content"],
            },
        )

    assert response.status_code == 400
    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    assert error["code"] == "INVALID_ARGUMENT"
    assert "deprecated and unsupported" in str(error)


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



def _episode_keeping_refs(
    *,
    documents: list[dict[str, str]],
    notes: str = "",
) -> Any:
    from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult

    refs: list[dict[str, str]] = []
    for doc in documents:
        ref = {"document_id": doc["document_id"]}
        if doc.get("chunk_id"):
            ref["chunk_id"] = doc["chunk_id"]
        if doc.get("section_path"):
            ref["section_path"] = doc["section_path"]
        refs.append(ref)
    return EpisodeResult(
        refs=refs,
        notes=notes,
        steps=[
            AgentStep(
                step_index=1,
                tool_name="finish",
                tool_args={"refs": refs},
                observation_text=notes or "done",
                error=None,
                elapsed_ms=1,
                tokens_used_delta=1,
                tokens_used_total=1,
            )
        ],
        stop_reason="finished",
        tokens_used=1,
        model_name="test",
    )


class _FakeHarness:
    def __init__(self, episode: Any) -> None:
        self._episode = episode

    async def run_episode(self, **_kwargs: Any) -> Any:
        return self._episode


def _patch_harness(monkeypatch: MonkeyPatch, episode: Any) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.agent_explore.harness.resolve_harness",
        lambda: _FakeHarness(episode),
    )


@pytest.mark.asyncio
async def test_agent_explore_retrieval_should_return_seeded_chunk_via_fake_episode(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        target = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-seed",
            source_file_name="target.pdf",
            section_path="Findings",
            content="explore seeded EBITDA marker content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-seed",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="unrelated filler content",
        )
        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(
                documents=[target],
                notes="explore seeded EBITDA marker content",
            ),
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-explore-seed",
                "query": "EBITDA marker",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["router_used"] == "agent_explore"
    assert response_json["stop_reason"] == "finished"
    assert isinstance(response_json.get("decision_trace"), list)
    assert response_json["decision_trace"]
    assert response_json["decision_trace"][-1]["phase"] == "finish"
    assert any(
        ref.get("chunk_id") == target["chunk_id"]
        and ref.get("document_id") == target["document_id"]
        for ref in referenced_chunks
    )
    assert results[0]["content"] == "explore seeded EBITDA marker content"
    assert results[0]["source"] == {
        "document_id": target["document_id"],
        "source_file_name": "target.pdf",
        "section_path": target["section_path"],
    }


@pytest.mark.asyncio
async def test_agentic_router_env_mapnav_is_ignored(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRIEVAL_AGENTIC_ROUTER", "mapnav")
    async with developer_api_client_factory() as api_client:
        target = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-env",
            source_file_name="target.pdf",
            section_path="Findings",
            content="env ignored content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-env",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="unrelated filler content",
        )
        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(documents=[target], notes="env ignored"),
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-explore-env",
                "query": "ignored",
                "top_k": 1,
                "use_agentic": True,
            },
        )
    assert response.status_code == 200
    assert cast(dict[str, object], response.json())["router_used"] == "agent_explore"


@pytest.mark.asyncio
async def test_agent_explore_should_not_hydrate_references_outside_request_scope(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-visible",
            source_file_name="visible.pdf",
            section_path="visible/section",
            content="visible scoped content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-visible",
            source_file_name="visible-filler.pdf",
            section_path="visible/filler",
            content="visible scoped filler content",
        )
        foreign = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-foreign",
            source_file_name="foreign.pdf",
            section_path="foreign/section",
            content="foreign scoped content should not leak",
        )
        _patch_harness(monkeypatch, _episode_keeping_refs(documents=[foreign]))

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-explore-visible",
                "query": "visible",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    assert response_json["router_used"] == "agent_explore"
    assert response_json["referenced_chunks"] == []
    assert response_json["results"] == []


@pytest.mark.asyncio
async def test_agent_explore_should_drop_references_with_mismatched_section_path(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        visible = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-section-mismatch",
            source_file_name="visible.pdf",
            section_path="visible/section",
            content="visible scoped content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-section-mismatch",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="filler content",
        )
        mismatched = {
            "document_id": visible["document_id"],
            "section_path": "wrong/section/path",
        }
        _patch_harness(monkeypatch, _episode_keeping_refs(documents=[mismatched]))

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-explore-section-mismatch",
                "query": "visible",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    assert response_json["router_used"] == "agent_explore"
    assert response_json["referenced_chunks"] == []
    assert response_json["results"] == []


@pytest.mark.asyncio
async def test_agent_explore_should_fail_when_final_hydration_db_fails(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    async def fail_final_hydration(**_kwargs: object) -> object:
        raise RuntimeError("forced final hydration database failure")

    async with developer_api_client_factory() as api_client:
        visible = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-hydration-failure",
            source_file_name="visible.pdf",
            section_path="visible/section",
            content="visible scoped content",
        )
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-hydration-failure",
            source_file_name="filler.pdf",
            section_path="filler/section",
            content="filler content",
        )
        from shared.services.retrieval.execution import routes as retrieval_routes

        _patch_harness(monkeypatch, _episode_keeping_refs(documents=[visible]))
        monkeypatch.setattr(
            retrieval_routes,
            "resolve_workflow_references",
            fail_final_hydration,
        )

        with pytest.raises(
            RuntimeError,
            match="forced final hydration database failure",
        ):
            await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": "contract-explore-hydration-failure",
                    "query": "visible",
                    "top_k": 1,
                    "use_agentic": True,
                },
            )


@pytest.mark.asyncio
async def test_agent_explore_should_preserve_same_chunk_id_across_documents(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    shared_chunk_id = f"chunk_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        first = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-shared-chunk",
            source_file_name="first.pdf",
            section_path="shared/first",
            content="first shared reference content",
            chunk_id=shared_chunk_id,
        )
        second_doc = await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace="contract-explore-shared-chunk",
            source_file_name="second.pdf",
            section_path="shared/second-host",
            content="host content for second document",
        )
        second = await _seed_retrieval_chunk_for_existing_document(
            user_id="local-dev-user",
            namespace="contract-explore-shared-chunk",
            document=second_doc,
            section_path="shared/second",
            content="second shared reference content",
            chunk_id=shared_chunk_id,
        )

        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(documents=[first, second]),
        )

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-explore-shared-chunk",
                "query": "shared reference",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    referenced_chunks = cast(list[dict[str, object]], response_json["referenced_chunks"])
    results = cast(list[dict[str, object]], response_json["results"])

    assert response_json["router_used"] == "agent_explore"
    assert len(referenced_chunks) == 2
    assert {ref["document_id"] for ref in referenced_chunks} == {
        first["document_id"],
        second["document_id"],
    }
    assert {ref["chunk_id"] for ref in referenced_chunks} == {shared_chunk_id}
    assert len(results) == 2
