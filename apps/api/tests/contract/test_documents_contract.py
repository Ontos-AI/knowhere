import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.support.contract_database import ContractDatabase
from shared.testing.contract_runtime import get_contract_database_url


async def _create_contract_engine() -> AsyncEngine:
    return create_async_engine(get_contract_database_url(), future=True)


async def _insert_document(
    *,
    document_id: str,
    user_id: str = "local-dev-user",
    namespace: str = "contract-documents",
    status: str = "active",
    source_file_name: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    engine = await _create_contract_engine()
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    effective_updated_at = updated_at or timestamp

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO documents (
                        document_id,
                        user_id,
                        namespace,
                        status,
                        current_job_result_id,
                        source_file_name,
                        created_at,
                        updated_at,
                        archived_at
                    ) VALUES (
                        :document_id,
                        :user_id,
                        :namespace,
                        :status,
                        :current_job_result_id,
                        :source_file_name,
                        :created_at,
                        :updated_at,
                        :archived_at
                    )
                    """),
                {
                    "document_id": document_id,
                    "user_id": user_id,
                    "namespace": namespace,
                    "status": status,
                    "current_job_result_id": None,
                    "source_file_name": source_file_name or f"{document_id}.pdf",
                    "created_at": timestamp,
                    "updated_at": effective_updated_at,
                    "archived_at": (
                        effective_updated_at if status == "archived" else None
                    ),
                },
            )
    finally:
        await engine.dispose()


async def _fetch_document(document_id: str) -> dict[str, object]:
    engine = await _create_contract_engine()
    try:
        async with engine.begin() as connection:
            document_row = (
                (
                    await connection.execute(
                        text("""
                        SELECT
                            document_id,
                            user_id,
                            namespace,
                            status,
                            current_job_result_id,
                            source_file_name,
                            archived_at
                        FROM documents
                        WHERE document_id = :document_id
                        """),
                        {"document_id": document_id},
                    )
                )
                .mappings()
                .one()
            )
            return dict(document_row)
    finally:
        await engine.dispose()


async def _fetch_graph_counts(
    *,
    document_id: str,
    peer_document_id: str | None = None,
) -> dict[str, int]:
    engine = await _create_contract_engine()
    document_node_id = f"doc:{document_id}"
    try:
        async with engine.begin() as connection:
            node_count = (
                await connection.execute(
                    text("""
                    SELECT COUNT(*)
                    FROM graph_nodes
                    WHERE owner_document_id = :document_id
                    """),
                    {"document_id": document_id},
                )
            ).scalar_one()
            related_edge_count = (
                await connection.execute(
                    text("""
                    SELECT COUNT(*)
                    FROM graph_edges
                    WHERE owner_document_id = :document_id
                       OR source_node_id = :document_node_id
                       OR target_node_id = :document_node_id
                    """),
                    {
                        "document_id": document_id,
                        "document_node_id": document_node_id,
                    },
                )
            ).scalar_one()
            peer_node_count = 0
            if peer_document_id is not None:
                peer_node_count = (
                    await connection.execute(
                        text("""
                        SELECT COUNT(*)
                        FROM graph_nodes
                        WHERE owner_document_id = :peer_document_id
                        """),
                        {"peer_document_id": peer_document_id},
                    )
                ).scalar_one()
            return {
                "nodes": int(node_count),
                "related_edges": int(related_edge_count),
                "peer_nodes": int(peer_node_count),
            }
    finally:
        await engine.dispose()


async def _insert_document_graph_fixture(
    *,
    document_id: str,
    job_result_id: str,
    peer_document_id: str,
    peer_job_result_id: str,
    user_id: str = "local-dev-user",
    namespace: str = "contract-documents",
) -> None:
    engine = await _create_contract_engine()
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO graph_nodes (
                    node_id,
                    user_id,
                    namespace,
                    node_kind,
                    owner_document_id,
                    job_result_id,
                    ref_document_id,
                    ref_section_id,
                    properties,
                    created_at,
                    updated_at
                ) VALUES
                    (:doc_node_id, :user_id, :namespace, 'document', :document_id, :job_result_id, :document_id, NULL, CAST('{}' AS JSON), :created_at, :updated_at),
                    (:peer_node_id, :user_id, :namespace, 'document', :peer_document_id, :peer_job_result_id, :peer_document_id, NULL, CAST('{}' AS JSON), :created_at, :updated_at)
                """),
                {
                    "doc_node_id": f"doc:{document_id}",
                    "peer_node_id": f"doc:{peer_document_id}",
                    "user_id": user_id,
                    "namespace": namespace,
                    "document_id": document_id,
                    "peer_document_id": peer_document_id,
                    "job_result_id": job_result_id,
                    "peer_job_result_id": peer_job_result_id,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            await connection.execute(
                text("""
                INSERT INTO graph_edges (
                    edge_id,
                    user_id,
                    namespace,
                    edge_kind,
                    source_node_id,
                    target_node_id,
                    owner_document_id,
                    job_result_id,
                    is_directed,
                    weight,
                    properties,
                    created_at,
                    updated_at
                ) VALUES
                    (:owned_edge_id, :user_id, :namespace, 'related', :doc_node_id, :peer_node_id, :document_id, :job_result_id, FALSE, 1.0, CAST('{}' AS JSON), :created_at, :updated_at),
                    (:incoming_edge_id, :user_id, :namespace, 'related', :peer_node_id, :doc_node_id, :peer_document_id, :peer_job_result_id, FALSE, 1.0, CAST('{}' AS JSON), :created_at, :updated_at)
                """),
                {
                    "owned_edge_id": f"edge_{uuid4().hex[:12]}",
                    "incoming_edge_id": f"edge_{uuid4().hex[:12]}",
                    "user_id": user_id,
                    "namespace": namespace,
                    "doc_node_id": f"doc:{document_id}",
                    "peer_node_id": f"doc:{peer_document_id}",
                    "document_id": document_id,
                    "peer_document_id": peer_document_id,
                    "job_result_id": job_result_id,
                    "peer_job_result_id": peer_job_result_id,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
    finally:
        await engine.dispose()


async def _insert_document_revision_with_chunks(
    *,
    document_id: str,
    chunks: list[dict[str, object]],
    namespace: str = "contract-documents",
    user_id: str = "local-dev-user",
    source_file_name: str = "contract-chunks.pdf",
) -> dict[str, str]:
    engine = await _create_contract_engine()
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    job_id = str(uuid4())
    job_result_id = str(uuid4())
    section_id = f"sec_{uuid4().hex[:12]}"

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO documents (
                        document_id,
                        user_id,
                        namespace,
                        status,
                        current_job_result_id,
                        source_file_name,
                        created_at,
                        updated_at,
                        archived_at
                    ) VALUES (
                        :document_id,
                        :user_id,
                        :namespace,
                        'active',
                        NULL,
                        :source_file_name,
                        :created_at,
                        :updated_at,
                        NULL
                    )
                    """),
                {
                    "document_id": document_id,
                    "user_id": user_id,
                    "namespace": namespace,
                    "source_file_name": source_file_name,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO jobs (
                        job_id,
                        user_id,
                        job_type,
                        status,
                        source_type,
                        webhook_enabled,
                        job_metadata,
                        version,
                        created_at,
                        updated_at,
                        credits_charged,
                        billing_status
                    ) VALUES (
                        :job_id,
                        :user_id,
                        'kb_management',
                        'done',
                        'url',
                        FALSE,
                        CAST(:job_metadata AS JSON),
                        0,
                        :created_at,
                        :updated_at,
                        0,
                        'pending'
                    )
                    """),
                {
                    "job_id": job_id,
                    "user_id": user_id,
                    "job_metadata": json.dumps({"document_id": document_id}),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO job_results (
                        id,
                        job_id,
                        document_id,
                        delivery_mode,
                        document_metadata,
                        inline_payload,
                        result_s3_key,
                        result_size,
                        created_at,
                        updated_at
                    ) VALUES (
                        :job_result_id,
                        :job_id,
                        :document_id,
                        'url',
                        CAST('{}' AS JSON),
                        CAST('{}' AS JSON),
                        :result_s3_key,
                        0,
                        :created_at,
                        :updated_at
                    )
                    """),
                {
                    "job_result_id": job_result_id,
                    "job_id": job_id,
                    "document_id": document_id,
                    "result_s3_key": f"results/{job_id}.zip",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO document_sections (
                        section_id,
                        user_id,
                        namespace,
                        document_id,
                        job_result_id,
                        parent_section_id,
                        section_path,
                        section_title,
                        section_level,
                        summary,
                        section_metadata,
                        sort_order,
                        created_at
                    ) VALUES (
                        :section_id,
                        :user_id,
                        :namespace,
                        :document_id,
                        :job_result_id,
                        NULL,
                        'Chapter 1',
                        'Chapter 1',
                        1,
                        NULL,
                        CAST('{}' AS JSON),
                        0,
                        :created_at
                    )
                    """),
                {
                    "section_id": section_id,
                    "user_id": user_id,
                    "namespace": namespace,
                    "document_id": document_id,
                    "job_result_id": job_result_id,
                    "created_at": timestamp,
                },
            )
            for sort_order, chunk in enumerate(chunks):
                await connection.execute(
                    text("""
                        INSERT INTO document_chunks (
                            id,
                            chunk_id,
                            user_id,
                            namespace,
                            document_id,
                            job_result_id,
                            section_id,
                            chunk_type,
                            content,
                            content_lexical_text,
                            path_lexical_text,
                            content_search_text,
                            path_search_text,
                            term_search_text,
                            source_chunk_path,
                            file_path,
                            chunk_metadata,
                            sort_order,
                            created_at
                        ) VALUES (
                            :id,
                            :chunk_id,
                            :user_id,
                            :namespace,
                            :document_id,
                            :job_result_id,
                            :section_id,
                            :chunk_type,
                            :content,
                            :content,
                            :section_path,
                            :content,
                            :section_path,
                            :content,
                            :source_chunk_path,
                            :file_path,
                            CAST(:chunk_metadata AS JSON),
                            :sort_order,
                            :created_at
                        )
                        """),
                    {
                        "id": chunk["id"],
                        "chunk_id": chunk["chunk_id"],
                        "user_id": user_id,
                        "namespace": namespace,
                        "document_id": document_id,
                        "job_result_id": job_result_id,
                        "section_id": section_id,
                        "chunk_type": chunk["chunk_type"],
                        "content": chunk.get("content"),
                        "section_path": "Chapter 1",
                        "source_chunk_path": chunk.get("source_chunk_path"),
                        "file_path": chunk.get("file_path"),
                        "chunk_metadata": json.dumps(chunk.get("metadata", {})),
                        "sort_order": sort_order,
                        "created_at": timestamp,
                    },
                )
            await connection.execute(
                text("""
                    UPDATE documents
                    SET current_job_result_id = :job_result_id
                    WHERE document_id = :document_id
                    """),
                {"document_id": document_id, "job_result_id": job_result_id},
            )
    finally:
        await engine.dispose()

    return {
        "job_id": job_id,
        "job_result_id": job_result_id,
        "section_id": section_id,
    }


@pytest.mark.asyncio
async def test_should_list_only_the_authenticated_users_documents_for_the_effective_namespace(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        other_user_id = f"contract-user-{uuid4().hex[:12]}"
        await ContractDatabase.insert_user(user_id=other_user_id)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        owned_first_document_id = f"doc_{uuid4().hex[:12]}"
        owned_second_document_id = f"doc_{uuid4().hex[:12]}"
        await _insert_document(
            document_id=owned_first_document_id,
            updated_at=now - timedelta(minutes=5),
        )
        await _insert_document(
            document_id=owned_second_document_id,
            updated_at=now,
        )
        await _insert_document(
            document_id=f"doc_{uuid4().hex[:12]}",
            namespace="other-namespace",
        )
        await _insert_document(
            document_id=f"doc_{uuid4().hex[:12]}",
            user_id=other_user_id,
        )
        await _insert_document(
            document_id=f"doc_{uuid4().hex[:12]}",
            status="archived",
        )

        default_namespace_response = await api_client.get("/api/v1/documents")
        named_namespace_response = await api_client.get(
            "/api/v1/documents",
            params={"namespace": "contract-documents"},
        )

    assert default_namespace_response.status_code == 200
    assert named_namespace_response.status_code == 200

    default_namespace_json = cast(dict[str, object], default_namespace_response.json())
    named_namespace_json = cast(dict[str, object], named_namespace_response.json())
    documents = cast(list[dict[str, object]], named_namespace_json["documents"])

    assert default_namespace_json == {
        "namespace": "default",
        "documents": [],
    }
    assert named_namespace_json["namespace"] == "contract-documents"
    assert [document["document_id"] for document in documents] == [
        owned_second_document_id,
        owned_first_document_id,
    ]
    assert all(document["namespace"] == "contract-documents" for document in documents)
    assert all(document["status"] == "active" for document in documents)


@pytest.mark.asyncio
async def test_should_return_document_details_for_an_owned_document(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        await _insert_document(
            document_id=document_id,
            source_file_name="contract-detail.pdf",
        )
        response = await api_client.get(f"/api/v1/documents/{document_id}")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())

    assert response_json["document_id"] == document_id
    assert response_json["namespace"] == "contract-documents"
    assert response_json["status"] == "active"
    assert response_json["current_job_result_id"] is None
    assert response_json["source_file_name"] == "contract-detail.pdf"
    assert response_json["created_at"]
    assert response_json["updated_at"]
    assert response_json["archived_at"] is None


@pytest.mark.asyncio
async def test_should_return_not_found_when_requesting_a_missing_document(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    missing_document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        response = await api_client.get(f"/api/v1/documents/{missing_document_id}")

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Document not found"
    assert error["details"] == {
        "resource": "Document",
        "id": missing_document_id,
    }


@pytest.mark.asyncio
async def test_should_list_current_document_chunks_by_document_id(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"
    first_chunk_id = f"dchk_{uuid4().hex[:12]}"
    second_chunk_id = f"dchk_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        revision = await _insert_document_revision_with_chunks(
            document_id=document_id,
            chunks=[
                {
                    "id": first_chunk_id,
                    "chunk_id": "parser-chunk-1",
                    "chunk_type": "text",
                    "content": "First chunk content",
                    "source_chunk_path": "Chapter 1/Intro",
                    "metadata": {"summary": "Intro", "page_nums": [1]},
                },
                {
                    "id": second_chunk_id,
                    "chunk_id": "parser-chunk-2",
                    "chunk_type": "table",
                    "content": "| A | B |",
                    "source_chunk_path": "Chapter 1/Table",
                    "file_path": "tables/table-1.html",
                    "metadata": {"summary": "Table", "page_nums": [2]},
                },
            ],
        )
        response = await api_client.get(
            f"/api/v1/documents/{document_id}/chunks",
            params={"page": 1, "page_size": 1},
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    chunks = cast(list[dict[str, object]], response_json["chunks"])

    assert response_json["document_id"] == document_id
    assert response_json["namespace"] == "contract-documents"
    assert response_json["job_id"] == revision["job_id"]
    assert response_json["job_result_id"] == revision["job_result_id"]
    assert response_json["pagination"] == {
        "page": 1,
        "page_size": 1,
        "total": 2,
        "total_pages": 2,
    }
    assert chunks == [
        {
            "id": first_chunk_id,
            "chunk_id": "parser-chunk-1",
            "chunk_type": "text",
            "content": "First chunk content",
            "section_id": revision["section_id"],
            "section_path": "Chapter 1",
            "source_chunk_path": "Chapter 1/Intro",
            "file_path": None,
            "sort_order": 0,
            "metadata": {"summary": "Intro", "page_nums": [1]},
            "asset_url": None,
            "created_at": chunks[0]["created_at"],
        }
    ]


@pytest.mark.asyncio
async def test_should_return_not_found_when_listing_chunks_for_missing_document(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    missing_document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        response = await api_client.get(
            f"/api/v1/documents/{missing_document_id}/chunks"
        )

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Document not found"
    assert error["details"] == {
        "resource": "Document",
        "id": missing_document_id,
    }


@pytest.mark.asyncio
async def test_should_return_one_document_chunk_by_document_chunk_id(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"
    chunk_id = f"dchk_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        revision = await _insert_document_revision_with_chunks(
            document_id=document_id,
            chunks=[
                {
                    "id": chunk_id,
                    "chunk_id": "parser-chunk-1",
                    "chunk_type": "image",
                    "content": "Figure summary",
                    "source_chunk_path": "Chapter 1/Figure",
                    "file_path": "images/figure-1.png",
                    "metadata": {"summary": "Figure", "page_nums": [3]},
                }
            ],
        )
        response = await api_client.get(
            f"/api/v1/documents/{document_id}/chunks/{chunk_id}",
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    chunk = cast(dict[str, object], response_json["chunk"])

    assert response_json["document_id"] == document_id
    assert response_json["namespace"] == "contract-documents"
    assert response_json["job_id"] == revision["job_id"]
    assert response_json["job_result_id"] == revision["job_result_id"]
    assert chunk["id"] == chunk_id
    assert chunk["chunk_id"] == "parser-chunk-1"
    assert chunk["chunk_type"] == "image"
    assert chunk["content"] == "Figure summary"
    assert chunk["section_id"] == revision["section_id"]
    assert chunk["section_path"] == "Chapter 1"
    assert chunk["source_chunk_path"] == "Chapter 1/Figure"
    assert chunk["file_path"] == "images/figure-1.png"
    assert chunk["metadata"] == {"summary": "Figure", "page_nums": [3]}
    assert chunk["asset_url"] is None
    assert chunk["created_at"]


@pytest.mark.asyncio
async def test_should_return_not_found_when_requesting_a_missing_document_chunk(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"
    missing_chunk_id = f"dchk_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        await _insert_document_revision_with_chunks(
            document_id=document_id,
            chunks=[
                {
                    "id": f"dchk_{uuid4().hex[:12]}",
                    "chunk_id": "parser-chunk-1",
                    "chunk_type": "text",
                    "content": "First chunk content",
                    "source_chunk_path": "Chapter 1/Intro",
                    "metadata": {"summary": "Intro", "page_nums": [1]},
                }
            ],
        )
        response = await api_client.get(
            f"/api/v1/documents/{document_id}/chunks/{missing_chunk_id}",
        )

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Document chunk not found"
    assert error["details"] == {
        "resource": "Document chunk",
        "id": missing_chunk_id,
    }


@pytest.mark.asyncio
async def test_should_archive_a_document_via_the_canonical_archive_route(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"
    peer_document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        document_revision = await _insert_document_revision_with_chunks(
            document_id=document_id,
            chunks=[
                {
                    "id": f"dchk_{uuid4().hex[:12]}",
                    "chunk_id": "archive-chunk-1",
                    "chunk_type": "text",
                    "content": "Archived graph chunk",
                    "source_chunk_path": "Chapter 1/Archive",
                    "metadata": {"keywords": ["archive"]},
                }
            ],
        )
        peer_revision = await _insert_document_revision_with_chunks(
            document_id=peer_document_id,
            chunks=[
                {
                    "id": f"dchk_{uuid4().hex[:12]}",
                    "chunk_id": "peer-chunk-1",
                    "chunk_type": "text",
                    "content": "Peer graph chunk",
                    "source_chunk_path": "Chapter 1/Peer",
                    "metadata": {"keywords": ["peer"]},
                }
            ],
        )
        await _insert_document_graph_fixture(
            document_id=document_id,
            job_result_id=document_revision["job_result_id"],
            peer_document_id=peer_document_id,
            peer_job_result_id=peer_revision["job_result_id"],
        )
        response = await api_client.post(f"/api/v1/documents/{document_id}/archive")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    persisted_document = await _fetch_document(document_id)
    graph_counts = await _fetch_graph_counts(
        document_id=document_id,
        peer_document_id=peer_document_id,
    )

    assert response_json["document_id"] == document_id
    assert response_json["status"] == "archived"
    assert response_json["archived_at"]
    assert persisted_document["status"] == "archived"
    assert persisted_document["archived_at"] is not None
    assert graph_counts == {
        "nodes": 0,
        "related_edges": 0,
        "peer_nodes": 1,
    }


@pytest.mark.asyncio
async def test_should_archive_a_document_via_the_legacy_archive_route(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        await _insert_document(document_id=document_id)
        response = await api_client.post(f"/api/v1/documents/{document_id}:archive")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    persisted_document = await _fetch_document(document_id)

    assert response_json["document_id"] == document_id
    assert response_json["status"] == "archived"
    assert response_json["archived_at"]
    assert persisted_document["status"] == "archived"
    assert persisted_document["archived_at"] is not None
