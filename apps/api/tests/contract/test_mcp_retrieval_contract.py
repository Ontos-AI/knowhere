import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
import importlib
import socket
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.mcp.retrieval_server import (
    are_mcp_timing_logs_enabled,
    create_retrieval_mcp_server,
    to_mcp_search_response,
)
from app.mcp.tool_runtime import MCP_TIMING_LOGS_ENABLED_ENV
from app.services.documents import inspection_service as inspection_module
from app.services.documents.inspection_service import DocumentInspectionService
from tests.support.contract_database import ContractDatabase

from shared.services.retrieval.outline_snapshot import (
    MCP_OUTLINE_SNAPSHOT_METADATA_KEY,
    MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION,
)
from shared.services.storage.result_storage import (
    DEFAULT_RESULT_ARTIFACT_URL_EXPIRES_IN_SECONDS,
    JobResultStorage,
)
from shared.services.storage.storage_adapter import StorageAdapter


@pytest.mark.asyncio
async def test_mcp_should_register_knowhere_tools_with_structured_outputs() -> None:
    server = create_retrieval_mcp_server()

    assert server.settings.json_response is True

    tools = await server.list_tools()
    tools_by_name = {tool.name: tool for tool in tools}

    assert set(tools_by_name) == {
        "knowhere_parse_url",
        "knowhere_get_job_status",
        "knowhere_search",
        "knowhere_list_documents",
        "knowhere_get_document_outline",
        "knowhere_read_chunks",
        "knowhere_grep_chunks",
    }
    assert "retrieval.query" not in tools_by_name
    assert all(tool.outputSchema for tool in tools)
    assert set(tools_by_name["knowhere_search"].inputSchema["properties"]) == {
        "query",
        "namespace",
        "top_k",
        "target_content",
        "signal_paths",
        "filter_mode",
        "threshold",
        "exclude_document_ids",
        "exclude_sections",
    }
    assert tools_by_name["knowhere_search"].outputSchema is not None
    assert set(tools_by_name["knowhere_search"].outputSchema["properties"]) == {
        "namespace",
        "query",
        "evidence_text",
        "referenced_chunks",
        "decision_trace",
        "results",
        "stop_reason",
        "failure_reason",
    }
    assert set(tools_by_name["knowhere_parse_url"].inputSchema["properties"]) == {
        "url",
        "namespace",
        "document_id",
        "data_id",
        "parse_track",
        "parsing_params",
    }
    parse_url_output_schema = tools_by_name["knowhere_parse_url"].outputSchema
    assert parse_url_output_schema is not None
    assert set(parse_url_output_schema["properties"]) == {
        "namespace",
        "job",
        "interpretation",
    }
    assert set(tools_by_name["knowhere_get_job_status"].inputSchema["properties"]) == {
        "job_id",
        "namespace",
    }
    job_status_output_schema = tools_by_name["knowhere_get_job_status"].outputSchema
    assert job_status_output_schema is not None
    assert set(job_status_output_schema["properties"]) == {
        "namespace",
        "job",
        "is_terminal",
        "is_success",
        "is_failure",
        "interpretation",
    }
    read_output_schema = tools_by_name["knowhere_read_chunks"].outputSchema
    assert read_output_schema is not None
    read_chunk_properties = read_output_schema["$defs"]["DocumentReadChunk"][
        "properties"
    ]
    assert "position" in read_chunk_properties
    assert "ordinal" not in read_chunk_properties
    assert "asset_url" in read_chunk_properties


def test_search_projection_should_preserve_structured_retrieval_fields() -> None:
    response = to_mcp_search_response(
        {
            "namespace": "contract-documents",
            "query": "revenue",
            "evidence_text": "Body > Revenue was $42M.",
            "referenced_chunks": [
                {
                    "document_id": "doc_contract",
                    "chunk_id": "semantic-1",
                    "chunk_type": "text",
                    "section_path": "Body",
                    "file_path": "source.md",
                    "job_id": "job_1",
                    "score": "0.91",
                }
            ],
            "decision_trace": [{"phase": "terminal", "result": {"status": "ok"}}],
            "results": [
                {
                    "chunk_type": "text",
                    "content": "Revenue was $42M.",
                    "score": 0.91,
                    "source": {
                        "document_id": "doc_contract",
                        "source_file_name": "report.md",
                        "section_path": "Body",
                    },
                }
            ],
            "stop_reason": "complete",
        },
        namespace="fallback",
        query="fallback",
    )

    assert response.namespace == "contract-documents"
    assert response.query == "revenue"
    assert response.referenced_chunks[0].document_id == "doc_contract"
    assert response.referenced_chunks[0].score == 0.91
    assert response.results[0].source.source_file_name == "report.md"
    assert response.decision_trace == [
        {"phase": "terminal", "result": {"status": "ok"}}
    ]
    assert response.stop_reason == "complete"
    assert response.failure_reason is None


def test_mcp_timing_logs_should_be_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MCP_TIMING_LOGS_ENABLED_ENV, raising=False)
    assert are_mcp_timing_logs_enabled() is False

    monkeypatch.setenv(MCP_TIMING_LOGS_ENABLED_ENV, "true")
    assert are_mcp_timing_logs_enabled() is True


def test_result_artifact_urls_should_default_to_seven_days() -> None:
    class FakeStorageAdapter:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def generate_presigned_url(
            self,
            key: str,
            expiration: int = 3600,
            bucket: str | None = None,
            method: str = "GET",
            headers: dict[str, str] | None = None,
        ) -> str:
            self.requests.append(
                {
                    "key": key,
                    "expiration": expiration,
                    "bucket": bucket,
                    "method": method,
                    "headers": headers,
                }
            )
            return f"https://assets.example.com/{key}?expires={expiration}"

    fake_adapter = FakeStorageAdapter()
    result_storage = JobResultStorage(
        results_bucket="result-bucket",
        storage_adapter=cast(StorageAdapter, fake_adapter),
    )

    asset_url = result_storage.generate_artifact_url(
        job_id="job-1",
        artifact_ref="images/chart.png",
    )

    assert asset_url == (
        "https://assets.example.com/results/job-1/images/chart.png?expires="
        f"{DEFAULT_RESULT_ARTIFACT_URL_EXPIRES_IN_SECONDS}"
    )
    assert fake_adapter.requests == [
        {
            "key": "results/job-1/images/chart.png",
            "expiration": DEFAULT_RESULT_ARTIFACT_URL_EXPIRES_IN_SECONDS,
            "bucket": "result-bucket",
            "method": "GET",
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_mcp_should_create_url_parse_job_and_return_job_status(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://example.com/contracts/knowhere-mcp.pdf"
    api_key = ""

    class FakeCeleryTask:
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name

        def apply_async(
            self,
            args: list[object] | None = None,
            kwargs: dict[str, object] | None = None,
        ) -> None:
            scheduled_tasks.append(
                {
                    "task_name": self.task_name,
                    "args": args or [],
                    "kwargs": kwargs or {},
                }
            )

    class FakeCeleryApp:
        def signature(self, task_name: str) -> FakeCeleryTask:
            return FakeCeleryTask(task_name)

    def resolve_public_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        del host, port, args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    import shared.core.celery_app as celery_app_module
    scheduled_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(socket, "getaddrinfo", resolve_public_address)
    monkeypatch.setattr(
        celery_app_module,
        "get_celery_app",
        lambda: FakeCeleryApp(),
    )

    async with developer_api_client_factory() as api_client:
        from shared.services.redis import JobInfoRedisService, JobMetadataService
        from shared.services.redis.redis_service_factory import RedisServiceFactory

        authorization = api_client.headers["Authorization"]
        api_key = authorization.removeprefix("Bearer ")
        runtime_retrieval_server = importlib.import_module("app.mcp.retrieval_server")
        server = runtime_retrieval_server.create_retrieval_mcp_server()
        context = _make_mcp_context(
            authorization=authorization,
            namespace="header-namespace",
        )

        parse_response = await server._tool_manager.call_tool(
            "knowhere_parse_url",
            {
                "url": source_url,
                "namespace": "contract-mcp-parse",
                "data_id": "mcp-parse-data",
                "parsing_params": {
                    "model": "base",
                    "ocr_enabled": True,
                    "smart_title_parse": False,
                },
            },
            context=context,
            convert_result=False,
        )

        assert parse_response.namespace == "contract-mcp-parse"
        assert parse_response.job.status == "waiting-file"
        assert parse_response.job.source_type == "url"
        assert parse_response.job.data_id == "mcp-parse-data"
        assert parse_response.job.upload_url is None
        assert parse_response.job.upload_headers is None
        assert parse_response.job.expires_in is None
        assert "Poll knowhere_get_job_status" in parse_response.interpretation

        job_id = parse_response.job.job_id
        job_row = await ContractDatabase.fetch_one(
            """
            SELECT user_id, job_type, status, source_type, s3_key, job_metadata
            FROM jobs
            WHERE job_id = :job_id
            """,
            {"job_id": job_id},
        )
        assert job_row is not None
        job_metadata = job_row["job_metadata"]
        assert job_row["user_id"] == "local-dev-user"
        assert job_row["job_type"] == "document_ingestion"
        assert job_row["status"] == "waiting-file"
        assert job_row["source_type"] == "url"
        assert job_row["s3_key"] == f"uploads/{job_id}.pdf"
        assert job_metadata["namespace"] == "contract-mcp-parse"
        assert job_metadata["source_url"] == source_url
        assert job_metadata["source_file_name"] == "knowhere-mcp.pdf"
        assert job_metadata["data_id"] == "mcp-parse-data"
        assert job_metadata["parsing_params"] == {
            "model": "base",
            "ocr_enabled": True,
            "doc_type": "auto",
            "smart_title_parse": False,
            "summary_image": True,
            "summary_table": True,
            "summary_txt": True,
            "add_frag_desc": "",
            "summary_use_llm": False,
        }

        redis_service = RedisServiceFactory.get_service()
        metadata_service = JobMetadataService(redis_service)
        job_info_service = JobInfoRedisService(redis_service)
        cached_metadata = await metadata_service.get_metadata(job_id)
        cached_job_info = await job_info_service.get_job_info(job_id)

        assert cached_metadata is not None
        assert cached_metadata["namespace"] == "contract-mcp-parse"
        assert cached_metadata["source_url"] == source_url
        assert cached_job_info is not None
        assert cached_job_info["job_id"] == job_id
        assert cached_job_info["user_id"] == "local-dev-user"

        status_response = await server._tool_manager.call_tool(
            "knowhere_get_job_status",
            {"job_id": job_id},
            context=context,
            convert_result=False,
        )

        explicit_namespace_status_response = await server._tool_manager.call_tool(
            "knowhere_get_job_status",
            {"job_id": job_id, "namespace": "explicit-status-namespace"},
            context=context,
            convert_result=False,
        )
        assert explicit_namespace_status_response.job.job_id == job_id

    assert api_key
    assert scheduled_tasks == [
        {
            "task_name": "app.core.tasks.document_ingestion_tasks.upload_url_file_task",
            "args": [job_id, source_url, "local-dev-user"],
            "kwargs": {"job_type": "document_ingestion"},
        }
    ]
    assert status_response.namespace == "contract-mcp-parse"
    assert status_response.job.job_id == job_id
    assert status_response.job.status == "waiting-file"
    assert status_response.job.file_name == "knowhere-mcp.pdf"
    assert status_response.job.file_extension == "PDF"
    assert status_response.is_terminal is False
    assert status_response.is_success is False
    assert status_response.is_failure is False
    assert "still running" in status_response.interpretation


@pytest.mark.asyncio
async def test_document_inspection_should_outline_active_revision_only(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        await _insert_document_revision_fixture(document_id=document_id)
        service = DocumentInspectionService()

        async def fail_chunk_scan(*args: object, **kwargs: object) -> object:
            raise AssertionError("outline should not load inspection chunk rows")

        monkeypatch.setattr(
            service._repository,
            "list_current_document_chunks_for_inspection",
            fail_chunk_scan,
        )
        async with get_db_context() as db:
            response = await service.get_document_outline(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
            )

    assert response is not None
    assert response.namespace == "contract-documents"
    assert response.document.document_id == document_id
    assert response.total_chunks == 4
    assert response.type_counts == {"text": 3, "table": 1}
    assert [
        {
            "path": section.section_path,
            "start": section.start_chunk,
            "end": section.end_chunk,
            "count": section.chunk_count,
            "types": section.type_counts,
        }
        for section in response.sections
    ] == [
        {
            "path": "Intro",
            "start": 1,
            "end": 1,
            "count": 1,
            "types": {"text": 1},
        },
        {
            "path": "Body",
            "start": 2,
            "end": 2,
            "count": 1,
            "types": {"text": 1},
        },
        {
            "path": "Body / Finance",
            "start": 3,
            "end": 4,
            "count": 2,
            "types": {"table": 1, "text": 1},
        },
    ]
    assert [
        {
            "path": section.section_path,
            "children": [child.section_path for child in section.children],
        }
        for section in response.section_tree
    ] == [
        {"path": "Intro", "children": []},
        {"path": "Body", "children": ["Body / Finance"]},
    ]
    assert response.section_tree[1].children[0].chunk_count == 2


@pytest.mark.asyncio
async def test_document_inspection_should_use_persisted_outline_snapshot(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        fixture = await _insert_document_revision_fixture(document_id=document_id)
        await _update_job_result_metadata(
            job_result_id=fixture["job_result_id"],
            document_metadata={
                MCP_OUTLINE_SNAPSHOT_METADATA_KEY: {
                    "schema_version": MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION,
                    "job_result_id": fixture["job_result_id"],
                    "job_id": fixture["job_id"],
                    "total_chunks": 99,
                    "type_counts": {"text": 98, "table": 1},
                    "sections": [
                        {
                            "section_id": "snapshot-parent",
                            "section_path": "Snapshot",
                            "section_title": "Snapshot",
                            "section_level": 1,
                            "summary": "Precomputed outline section.",
                            "start_chunk": None,
                            "end_chunk": None,
                            "chunk_count": 0,
                            "type_counts": {},
                        },
                        {
                            "section_id": "snapshot-child",
                            "section_path": "Snapshot / Child",
                            "section_title": "Child",
                            "section_level": 2,
                            "summary": "Precomputed child section.",
                            "start_chunk": 10,
                            "end_chunk": 20,
                            "chunk_count": 11,
                            "type_counts": {"text": 11},
                        },
                    ],
                    "section_tree": [
                        {
                            "section_id": "snapshot-parent",
                            "section_path": "Snapshot",
                            "section_title": "Snapshot",
                            "section_level": 1,
                            "summary": "Precomputed outline section.",
                            "start_chunk": None,
                            "end_chunk": None,
                            "chunk_count": 0,
                            "type_counts": {},
                            "children": [
                                {
                                    "section_id": "snapshot-child",
                                    "section_path": "Snapshot / Child",
                                    "section_title": "Child",
                                    "section_level": 2,
                                    "summary": "Precomputed child section.",
                                    "start_chunk": 10,
                                    "end_chunk": 20,
                                    "chunk_count": 11,
                                    "type_counts": {"text": 11},
                                    "children": [],
                                }
                            ],
                        },
                    ],
                }
            },
        )
        service = DocumentInspectionService()

        async def fail_live_stats(*args: object, **kwargs: object) -> object:
            raise AssertionError("outline snapshot should bypass live stats")

        monkeypatch.setattr(
            service._repository,
            "get_current_document_outline_chunk_stats",
            fail_live_stats,
        )
        async with get_db_context() as db:
            response = await service.get_document_outline(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
            )

    assert response is not None
    assert response.job_result_id == fixture["job_result_id"]
    assert response.job_id == fixture["job_id"]
    assert response.total_chunks == 99
    assert response.type_counts == {"text": 98, "table": 1}
    assert [section.section_path for section in response.sections] == [
        "Snapshot",
        "Snapshot / Child",
    ]
    assert response.sections[1].chunk_count == 11
    assert [section.section_path for section in response.section_tree] == ["Snapshot"]
    assert response.section_tree[0].children[0].section_path == "Snapshot / Child"


@pytest.mark.asyncio
async def test_document_inspection_should_fallback_when_outline_snapshot_is_invalid(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        fixture = await _insert_document_revision_fixture(document_id=document_id)
        await _update_job_result_metadata(
            job_result_id=fixture["job_result_id"],
            document_metadata={
                MCP_OUTLINE_SNAPSHOT_METADATA_KEY: {
                    "schema_version": MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION,
                    "job_result_id": "old-job-result",
                    "job_id": "old-job",
                    "total_chunks": 1,
                    "type_counts": {"text": 1},
                    "sections": [],
                }
            },
        )
        service = DocumentInspectionService()
        async with get_db_context() as db:
            response = await service.get_document_outline(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
            )

    assert response is not None
    assert response.job_result_id == fixture["job_result_id"]
    assert response.total_chunks == 4
    assert [section.section_path for section in response.sections] == [
        "Intro",
        "Body",
        "Body / Finance",
    ]
    assert response.section_tree[1].children[0].section_path == "Body / Finance"


@pytest.mark.asyncio
async def test_publication_should_store_outline_snapshot_in_job_result_metadata(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"
    job_id = str(uuid4())
    job_result_id = str(uuid4())

    async with developer_api_client_factory():
        from shared.core.database_sync import get_sync_db_context
        from shared.services.retrieval.publication_service import (
            RetrievalPublicationService,
        )

        await _insert_job(job_id=job_id, document_id=document_id)
        await _insert_job_result(
            job_result_id=job_result_id,
            job_id=job_id,
            document_id=None,
            document_metadata={"existing": "kept"},
        )
        with get_sync_db_context() as db:
            published = RetrievalPublicationService().publish_document_state(
                db,
                job_id=job_id,
                job_result_id=job_result_id,
                chunks=[
                    {
                        "chunk_id": "semantic-intro",
                        "type": "text",
                        "content": "Alpha introduction.",
                        "path": "contract-report.md/Intro",
                    },
                    {
                        "chunk_id": "semantic-finance-table",
                        "type": "table",
                        "content": "<table><tr><td>Revenue</td></tr></table>",
                        "path": "contract-report.md/Body/Finance",
                    },
                ],
            )

        metadata_row = await ContractDatabase.fetch_one(
            """
            SELECT document_metadata
            FROM job_results
            WHERE id = :job_result_id
            """,
            {"job_result_id": job_result_id},
        )

    assert published is not None
    assert published.document_id is not None
    assert metadata_row is not None
    metadata = metadata_row["document_metadata"]
    snapshot = metadata[MCP_OUTLINE_SNAPSHOT_METADATA_KEY]
    assert metadata["existing"] == "kept"
    assert snapshot["schema_version"] == MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION
    assert snapshot["job_result_id"] == job_result_id
    assert snapshot["job_id"] == job_id
    assert snapshot["total_chunks"] == 2
    assert snapshot["type_counts"] == {"table": 1, "text": 1}
    assert [
        {
            "path": section["section_path"],
            "count": section["chunk_count"],
            "types": section["type_counts"],
        }
        for section in snapshot["sections"]
    ] == [
        {"path": "Intro", "count": 1, "types": {"text": 1}},
        {"path": "Body", "count": 0, "types": {}},
        {"path": "Body / Finance", "count": 1, "types": {"table": 1}},
    ]
    assert [
        {
            "path": section["section_path"],
            "children": [
                child["section_path"] for child in section.get("children", [])
            ],
        }
        for section in snapshot["section_tree"]
    ] == [
        {"path": "Intro", "children": []},
        {"path": "Body", "children": ["Body / Finance"]},
    ]


@pytest.mark.asyncio
async def test_document_inspection_should_read_by_range_section_and_ids(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    class FakeResultStorage:
        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str | None:
            del expires_in
            return f"https://assets.example.com/{job_id}/{artifact_ref}?refresh=1"

        def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
            if not artifact_ref:
                return None
            normalized = artifact_ref.strip().replace("\\", "/").lstrip("/")
            root_directory = normalized.split("/", 1)[0]
            if root_directory not in {"images", "tables"}:
                return None
            return normalized

    def fake_get_result_storage() -> FakeResultStorage:
        return FakeResultStorage()

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        monkeypatch.setattr(
            "shared.services.retrieval.hydration.assets.get_result_storage",
            fake_get_result_storage,
        )
        fixture = await _insert_document_revision_fixture(document_id=document_id)
        service = DocumentInspectionService()
        async with get_db_context() as db:
            range_response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                start_chunk=2,
                end_chunk=3,
            )
            section_response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                section_path="Body / Finance",
            )
            id_response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                document_chunk_id=fixture["finance_table_chunk_id"],
            )
            semantic_id_response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                chunk_id="semantic-body-text",
            )

    assert range_response is not None
    assert [chunk.position for chunk in range_response.chunks] == [2, 3]
    assert range_response.chunks[0].asset_url is None
    assert range_response.chunks[1].asset_url == (
        f"https://assets.example.com/{fixture['job_id']}/tables/revenue.html?refresh=1"
    )
    assert range_response.next_chunk == 4

    assert section_response is not None
    assert [chunk.position for chunk in section_response.chunks] == [3, 4]
    assert section_response.chunks[0].asset_url == (
        f"https://assets.example.com/{fixture['job_id']}/tables/revenue.html?refresh=1"
    )
    assert section_response.chunks[1].asset_url is None
    assert section_response.next_chunk is None

    assert id_response is not None
    assert [chunk.document_chunk_id for chunk in id_response.chunks] == [
        fixture["finance_table_chunk_id"]
    ]
    assert id_response.chunks[0].asset_url == (
        f"https://assets.example.com/{fixture['job_id']}/tables/revenue.html?refresh=1"
    )
    assert id_response.next_chunk is None

    assert semantic_id_response is not None
    assert [chunk.chunk_id for chunk in semantic_id_response.chunks] == [
        "semantic-body-text"
    ]
    assert semantic_id_response.chunks[0].asset_url is None


@pytest.mark.asyncio
async def test_document_inspection_should_ignore_read_asset_url_generation_failure(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    class FailingResultStorage:
        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str | None:
            del job_id, artifact_ref, expires_in
            raise RuntimeError("storage unavailable")

        def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
            if not artifact_ref:
                return None
            return artifact_ref.strip().replace("\\", "/").lstrip("/") or None

    def fake_get_result_storage() -> FailingResultStorage:
        return FailingResultStorage()

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        monkeypatch.setattr(
            "shared.services.retrieval.hydration.assets.get_result_storage",
            fake_get_result_storage,
        )
        fixture = await _insert_document_revision_fixture(document_id=document_id)
        service = DocumentInspectionService()
        async with get_db_context() as db:
            response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                document_chunk_id=fixture["finance_table_chunk_id"],
            )

    assert response is not None
    assert len(response.chunks) == 1
    assert response.chunks[0].file_path == "tables/revenue.html"
    assert response.chunks[0].asset_url is None


@pytest.mark.asyncio
async def test_document_inspection_should_scope_duplicate_chunk_id_reads(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        fixture = await _insert_document_revision_fixture(document_id=document_id)
        await _insert_chunk(
            document_chunk_id=f"dchk_{uuid4().hex[:12]}",
            semantic_chunk_id="semantic-body-text",
            document_id=document_id,
            job_result_id=fixture["job_result_id"],
            section_id=fixture["finance_section_id"],
            section_path="Body / Finance",
            chunk_type="text",
            content="Duplicate semantic id in the finance section.",
            source_chunk_path="Body / Finance/Duplicate",
            file_path=None,
            sort_order=4,
        )
        service = DocumentInspectionService()
        async with get_db_context() as db:
            scoped_response = await service.read_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                chunk_id="semantic-body-text",
                section_path="Body / Finance",
            )

    assert scoped_response is not None
    assert [chunk.section_path for chunk in scoped_response.chunks] == [
        "Body / Finance"
    ]
    assert scoped_response.chunks[0].content == (
        "Duplicate semantic id in the finance section."
    )


@pytest.mark.asyncio
async def test_document_inspection_should_grep_literal_regex_filters_and_caps(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = f"doc_{uuid4().hex[:12]}"

    async with developer_api_client_factory():
        from shared.core.database import get_db_context

        await _insert_document_revision_fixture(document_id=document_id)
        service = DocumentInspectionService()

        async def fail_chunk_scan(*args: object, **kwargs: object) -> object:
            raise AssertionError("grep should not load inspection chunk rows")

        monkeypatch.setattr(
            service._repository,
            "list_current_document_chunks_for_inspection",
            fail_chunk_scan,
        )
        async with get_db_context() as db:
            literal_response = await service.grep_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                pattern="revenue",
                max_results=1,
            )
            regex_response = await service.grep_chunks(
                db,
                user_id="local-dev-user",
                namespace="contract-documents",
                document_id=document_id,
                pattern="Revenue|Margin",
                is_regex=True,
                is_case_sensitive=True,
                section_path_prefix="Body / Finance",
                chunk_type="text",
            )
            wrong_namespace_response = await service.grep_chunks(
                db,
                user_id="local-dev-user",
                namespace="other-namespace",
                document_id=document_id,
                pattern="revenue",
            )

    assert literal_response is not None
    assert len(literal_response.matches) == 1
    assert literal_response.matches[0].position == 2
    assert literal_response.matches[0].snippet == "Revenue was $42M in 2026."
    assert literal_response.truncated is True
    assert literal_response.scanned_chunks == 2

    assert regex_response is not None
    assert [match.position for match in regex_response.matches] == [4]
    assert regex_response.matches[0].section_path == "Body / Finance"
    assert regex_response.truncated is False
    assert regex_response.scanned_chunks == 1

    assert wrong_namespace_response is None


def test_document_inspection_should_timeout_catastrophic_regex() -> None:
    matcher = inspection_module._create_chunk_matcher(
        pattern="(a+)+$",
        is_regex=True,
        is_case_sensitive=True,
    )

    with pytest.raises(ValueError, match="Regex grep timed out"):
        matcher(("a" * 10_000) + "!")


def _make_mcp_context(*, authorization: str, namespace: str | None = None) -> Any:
    headers = {"authorization": authorization}
    if namespace is not None:
        headers["x-knowhere-namespace"] = namespace
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers=headers),
        ),
    )


async def _insert_document_revision_fixture(*, document_id: str) -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    previous_job_id = str(uuid4())
    previous_job_result_id = str(uuid4())
    current_job_id = str(uuid4())
    current_job_result_id = str(uuid4())
    intro_section_id = f"sec_{uuid4().hex[:12]}"
    body_section_id = f"sec_{uuid4().hex[:12]}"
    finance_section_id = f"sec_{uuid4().hex[:12]}"
    finance_table_chunk_id = f"dchk_{uuid4().hex[:12]}"

    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id,
            user_id,
            namespace,
            status,
            current_job_result_id,
            source_file_name,
            parse_track,
            created_at,
            updated_at,
            archived_at
        ) VALUES (
            :document_id,
            'local-dev-user',
            'contract-documents',
            'active',
            NULL,
            'contract-report.md',
            'chunk',
            :created_at,
            :updated_at,
            NULL
        )
        """,
        {
            "document_id": document_id,
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )
    await _insert_job(job_id=previous_job_id, document_id=document_id)
    await _insert_job_result(
        job_result_id=previous_job_result_id,
        job_id=previous_job_id,
        document_id=document_id,
    )
    await _insert_section(
        section_id=f"sec_{uuid4().hex[:12]}",
        document_id=document_id,
        job_result_id=previous_job_result_id,
        section_path="Old Revision",
        section_title="Old Revision",
        parent_section_id=None,
        sort_order=0,
    )
    await _insert_chunk(
        document_chunk_id=f"dchk_{uuid4().hex[:12]}",
        semantic_chunk_id="semantic-old",
        document_id=document_id,
        job_result_id=previous_job_result_id,
        section_id=None,
        section_path="Old Revision",
        chunk_type="text",
        content="Old revision must not appear.",
        source_chunk_path="Old Revision",
        file_path=None,
        sort_order=0,
    )

    await _insert_job(job_id=current_job_id, document_id=document_id)
    await _insert_job_result(
        job_result_id=current_job_result_id,
        job_id=current_job_id,
        document_id=document_id,
    )
    await _insert_section(
        section_id=intro_section_id,
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_path="Intro",
        section_title="Intro",
        parent_section_id=None,
        sort_order=0,
    )
    await _insert_section(
        section_id=body_section_id,
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_path="Body",
        section_title="Body",
        parent_section_id=None,
        sort_order=1,
    )
    await _insert_section(
        section_id=finance_section_id,
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_path="Body / Finance",
        section_title="Finance",
        parent_section_id=body_section_id,
        sort_order=2,
    )
    await _insert_chunk(
        document_chunk_id=f"dchk_{uuid4().hex[:12]}",
        semantic_chunk_id="semantic-intro",
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_id=intro_section_id,
        section_path="Intro",
        chunk_type="text",
        content="Alpha introduction.",
        source_chunk_path="Intro",
        file_path=None,
        sort_order=0,
    )
    await _insert_chunk(
        document_chunk_id=f"dchk_{uuid4().hex[:12]}",
        semantic_chunk_id="semantic-body-text",
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_id=body_section_id,
        section_path="Body",
        chunk_type="text",
        content="Revenue was $42M in 2026.",
        source_chunk_path="Body",
        file_path=None,
        sort_order=1,
    )
    await _insert_chunk(
        document_chunk_id=finance_table_chunk_id,
        semantic_chunk_id="semantic-finance-table",
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_id=finance_section_id,
        section_path="Body / Finance",
        chunk_type="table",
        content="<table><tr><td>Revenue</td></tr></table>",
        source_chunk_path="Body / Finance/Table",
        file_path="tables/revenue.html",
        sort_order=2,
    )
    await _insert_chunk(
        document_chunk_id=f"dchk_{uuid4().hex[:12]}",
        semantic_chunk_id="semantic-finance-text",
        document_id=document_id,
        job_result_id=current_job_result_id,
        section_id=finance_section_id,
        section_path="Body / Finance",
        chunk_type="text",
        content="Margin guidance improved.",
        source_chunk_path="Body / Finance/Text",
        file_path=None,
        sort_order=3,
    )
    await ContractDatabase.execute(
        """
        UPDATE documents
        SET current_job_result_id = :job_result_id
        WHERE document_id = :document_id
        """,
        {"document_id": document_id, "job_result_id": current_job_result_id},
    )

    return {
        "job_id": current_job_id,
        "job_result_id": current_job_result_id,
        "finance_section_id": finance_section_id,
        "finance_table_chunk_id": finance_table_chunk_id,
    }


async def _insert_job(*, job_id: str, document_id: str) -> None:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    await ContractDatabase.execute(
        """
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
            'local-dev-user',
            'document_ingestion',
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
        """,
        {
            "job_id": job_id,
            "job_metadata": json.dumps({"document_id": document_id}),
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )


async def _insert_job_result(
    *,
    job_result_id: str,
    job_id: str,
    document_id: str | None,
    document_metadata: dict[str, object] | None = None,
) -> None:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    await ContractDatabase.execute(
        """
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
            CAST(:document_metadata AS JSON),
            CAST('{}' AS JSON),
            :result_s3_key,
            0,
            :created_at,
            :updated_at
        )
        """,
        {
            "job_result_id": job_result_id,
            "job_id": job_id,
            "document_id": document_id,
            "document_metadata": json.dumps(document_metadata or {}),
            "result_s3_key": f"results/{job_id}.zip",
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    )


async def _update_job_result_metadata(
    *,
    job_result_id: str,
    document_metadata: dict[str, object],
) -> None:
    await ContractDatabase.execute(
        """
        UPDATE job_results
        SET document_metadata = CAST(:document_metadata AS JSON)
        WHERE id = :job_result_id
        """,
        {
            "job_result_id": job_result_id,
            "document_metadata": json.dumps(document_metadata),
        },
    )


async def _insert_section(
    *,
    section_id: str,
    document_id: str,
    job_result_id: str,
    section_path: str,
    section_title: str,
    parent_section_id: str | None,
    sort_order: int,
) -> None:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    await ContractDatabase.execute(
        """
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
            'local-dev-user',
            'contract-documents',
            :document_id,
            :job_result_id,
            :parent_section_id,
            :section_path,
            :section_title,
            :section_level,
            :summary,
            CAST('{}' AS JSON),
            :sort_order,
            :created_at
        )
        """,
        {
            "section_id": section_id,
            "document_id": document_id,
            "job_result_id": job_result_id,
            "parent_section_id": parent_section_id,
            "section_path": section_path,
            "section_title": section_title,
            "section_level": section_path.count(" / ") + 1,
            "summary": f"Summary for {section_title}",
            "sort_order": sort_order,
            "created_at": timestamp,
        },
    )


async def _insert_chunk(
    *,
    document_chunk_id: str,
    semantic_chunk_id: str,
    document_id: str,
    job_result_id: str,
    section_id: str | None,
    section_path: str,
    chunk_type: str,
    content: str,
    source_chunk_path: str,
    file_path: str | None,
    sort_order: int,
) -> None:
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    await ContractDatabase.execute(
        """
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
            position,
            created_at
        ) VALUES (
            :id,
            :chunk_id,
            'local-dev-user',
            'contract-documents',
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
            :position,
            :created_at
        )
        """,
        {
            "id": document_chunk_id,
            "chunk_id": semantic_chunk_id,
            "document_id": document_id,
            "job_result_id": job_result_id,
            "section_id": section_id,
            "chunk_type": chunk_type,
            "content": content,
            "section_path": section_path,
            "source_chunk_path": source_chunk_path,
            "file_path": file_path,
            "chunk_metadata": json.dumps({"summary": source_chunk_path}),
            "sort_order": sort_order,
            "position": sort_order + 1,
            "created_at": timestamp,
        },
    )
