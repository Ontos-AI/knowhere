from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.support.contract_database import ContractDatabase


async def _seed_retrieval_document(
    *,
    user_id: str,
    namespace: str,
    source_file_name: str,
    section_path: str,
    content: str,
) -> dict[str, str]:
    document_id = f"doc_{uuid4().hex[:12]}"
    job_id = f"job_{uuid4().hex[:12]}"
    job_result_id = str(uuid4())
    section_id = f"sec_{uuid4().hex[:12]}"
    chunk_id = f"chunk_{uuid4().hex[:12]}"

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
        chunk_id=chunk_id,
        user_id=user_id,
        namespace=namespace,
        document_id=document_id,
        job_result_id=job_result_id,
        section_id=section_id,
        chunk_type="text",
        content=content,
        section_path=section_path,
    )

    return {
        "document_id": document_id,
        "job_id": job_id,
        "job_result_id": job_result_id,
        "section_id": section_id,
        "chunk_id": chunk_id,
        "section_path": section_path,
    }


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
    assert response_json["router_used"] == "small_kb_all"
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
    assert results[0]["source"]["document_id"] == seeded_document["document_id"]


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
    assert results[0]["source"]["document_id"] == included_document["document_id"]


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
    assert results[0]["source"]["document_id"] == included_document["document_id"]
    assert results[0]["source"]["section_path"] == included_document["section_path"]
