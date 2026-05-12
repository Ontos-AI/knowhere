from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
import json
import socket
from typing import cast
from uuid import uuid4

import jwt
import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.testing.contract_runtime import get_contract_database_url


async def _create_contract_engine() -> AsyncEngine:
    return create_async_engine(get_contract_database_url(), future=True)


async def _count_jobs() -> int:
    engine = await _create_contract_engine()
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text("SELECT COUNT(*) FROM jobs"))
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _load_job_record(job_id: str) -> dict[str, object]:
    engine = await _create_contract_engine()
    try:
        async with engine.begin() as connection:
            job_row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            user_id,
                            job_type,
                            status,
                            source_type,
                            s3_key,
                            webhook_url,
                            webhook_enabled,
                            job_metadata
                        FROM jobs
                        WHERE job_id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
            ).mappings().one()
            return dict(job_row)
    finally:
        await engine.dispose()


async def _insert_document(
    *,
    document_id: str,
    user_id: str = "local-dev-user",
    namespace: str = "contract-jobs",
    status: str = "active",
) -> None:
    engine = await _create_contract_engine()
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO documents (
                        document_id,
                        user_id,
                        namespace,
                        status,
                        source_file_name,
                        created_at,
                        updated_at,
                        archived_at
                    ) VALUES (
                        :document_id,
                        :user_id,
                        :namespace,
                        :status,
                        :source_file_name,
                        :created_at,
                        :updated_at,
                        :archived_at
                    )
                    """
                ),
                {
                    "document_id": document_id,
                    "user_id": user_id,
                    "namespace": namespace,
                    "status": status,
                    "source_file_name": f"{document_id}.pdf",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "archived_at": timestamp if status == "archived" else None,
                },
            )
    finally:
        await engine.dispose()


async def _insert_active_job(
    *,
    job_id: str,
    document_id: str,
    user_id: str = "local-dev-user",
    namespace: str = "contract-jobs",
    status: str = "running",
) -> None:
    engine = await _create_contract_engine()
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    job_metadata: dict[str, str] = {
        "document_id": document_id,
        "namespace": namespace,
        "source_type": "file",
    }

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
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
                        :user_id,
                        :job_type,
                        :status,
                        :source_type,
                        :webhook_enabled,
                        CAST(:job_metadata AS JSON),
                        :version,
                        :created_at,
                        :updated_at,
                        :credits_charged,
                        :billing_status
                    )
                    """
                ),
                {
                    "job_id": job_id,
                    "user_id": user_id,
                    "job_type": "kb_management",
                    "status": status,
                    "source_type": "file",
                    "webhook_enabled": False,
                    "job_metadata": json.dumps(job_metadata),
                    "version": 0,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "credits_charged": 0,
                    "billing_status": "pending",
                },
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_should_create_a_waiting_file_job_for_an_authenticated_developer(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.pdf",
        "data_id": "contract-job-file-upload",
    }

    async with developer_api_client_factory() as api_client:
        from shared.services.redis import JobInfoRedisService, JobMetadataService
        from shared.services.redis.redis_service_factory import RedisServiceFactory

        response = await api_client.post("/api/v1/jobs", json=payload)

        assert response.status_code == 200

        response_json: dict[str, object] = response.json()
        job_id = cast(str, response_json["job_id"])

        assert job_id.startswith("job_")
        assert response_json["status"] == "waiting-file"
        assert response_json["source_type"] == "file"
        assert response_json["namespace"] == payload["namespace"]
        assert response_json["data_id"] == payload["data_id"]
        assert response_json["document_id"] is None
        assert response_json["upload_url"]
        assert response_json["upload_headers"] == {"Content-Type": "application/pdf"}
        assert response_json["expires_in"]
        assert response_json["created_at"]

        engine = await _create_contract_engine()
        try:
            async with engine.begin() as connection:
                job_row = (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                user_id,
                                job_type,
                                status,
                                source_type,
                                s3_key,
                                webhook_enabled,
                                job_metadata
                            FROM jobs
                            WHERE job_id = :job_id
                            """
                        ),
                        {"job_id": job_id},
                    )
                ).mappings().one()
        finally:
            await engine.dispose()

        job_metadata = cast(dict[str, object], job_row["job_metadata"])
        persisted_document_id = cast(str, job_metadata["document_id"])
        original_request = cast(dict[str, object], job_metadata["original_request"])

        assert job_row["user_id"] == "local-dev-user"
        assert job_row["job_type"] == "kb_management"
        assert job_row["status"] == "waiting-file"
        assert job_row["source_type"] == "file"
        assert job_row["s3_key"] == f"uploads/{job_id}.pdf"
        assert job_row["webhook_enabled"] is False
        assert persisted_document_id.startswith("doc_")
        assert job_metadata["namespace"] == payload["namespace"]
        assert job_metadata["source_type"] == "file"
        assert job_metadata["source_file_name"] == payload["file_name"]
        assert job_metadata["data_id"] == payload["data_id"]
        assert original_request["file_name"] == payload["file_name"]
        assert original_request["source_type"] == payload["source_type"]

        redis_service = RedisServiceFactory.get_service()
        metadata_service = JobMetadataService(redis_service)
        job_info_service = JobInfoRedisService(redis_service)

        cached_metadata = await metadata_service.get_metadata(job_id)
        cached_job_info = await job_info_service.get_job_info(job_id)

        assert cached_metadata is not None
        assert cached_job_info is not None
        assert cached_metadata["document_id"] == persisted_document_id
        assert cached_metadata["namespace"] == payload["namespace"]
        assert cached_metadata["source_type"] == "file"
        assert cached_metadata["source_file_name"] == payload["file_name"]
        assert cached_job_info["job_id"] == job_id
        assert cached_job_info["user_id"] == "local-dev-user"
        assert cached_job_info["job_type"] == "kb_management"
        assert cached_job_info["source_type"] == "file"
        assert cached_job_info["s3_key"] == f"uploads/{job_id}.pdf"
        assert cached_job_info["webhook_enabled"] is False


@pytest.mark.asyncio
async def test_should_return_invalid_argument_when_file_mode_job_is_missing_file_name(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "data_id": "contract-job-missing-file-name",
    }

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"] == "file_name is required when source_type is 'file'"
    assert violations == [
        {
            "field": "file_name",
            "description": "Required for file source type",
        }
    ]
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_return_invalid_argument_when_file_mode_job_uses_an_unsupported_file_type(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.exe",
        "data_id": "contract-job-unsupported-file-type",
    }

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert str(error["message"]).startswith(
        "Unsupported file type. Supported formats:"
    )
    assert violations == [
        {
            "field": "file_name",
            "description": "File type not supported",
        }
    ]
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_require_authorization_when_creating_a_job(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.pdf",
        "data_id": "contract-job-missing-authorization",
    }

    async with api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 401
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == "Authentication required. Provide Authorization header."
    assert "details" not in error
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_reject_a_malformed_authorization_header_when_creating_a_job(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.pdf",
        "data_id": "contract-job-malformed-authorization",
    }

    async with api_client_factory() as api_client:
        api_client.headers.update({"Authorization": "bad-token"})
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 401
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == "Invalid Authorization header format"
    assert "details" not in error
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_reject_authenticated_user_id_missing_from_user_table(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    user_id = f"contract-missing-user-{uuid4().hex[:12]}"
    jwt_secret = f"contract-jwt-secret-{uuid4().hex[:12]}"
    token = jwt.encode(
        {
            "id": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        jwt_secret,
        algorithm="HS256",
    )
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.pdf",
        "data_id": "contract-job-missing-user",
    }

    async with api_client_factory() as api_client:
        from app.core import dependencies as auth_dependencies

        monkeypatch.setattr(
            auth_dependencies,
            "_get_verification_key",
            lambda _token: jwt_secret,
        )

        api_client.headers.update({"Authorization": f"Bearer {token}"})
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 401
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == "Invalid authentication credentials"
    assert "details" not in error
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_return_conflict_when_creating_a_job_for_a_document_with_an_active_ingestion_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_contract_{uuid4().hex[:12]}"
    active_job_id = f"job_contract_{uuid4().hex[:12]}"

    payload: dict[str, str] = {
        "document_id": document_id,
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "conflict-upload.pdf",
        "data_id": "contract-job-conflict",
    }

    async with developer_api_client_factory() as api_client:
        await _insert_document(document_id=document_id)
        await _insert_active_job(job_id=active_job_id, document_id=document_id)
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 409
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "ABORTED"
    assert (
        error["message"]
        == f"Document already has an active ingestion job. Active job: {active_job_id}."
    )
    assert error["details"] == {
        "reason": "ABORTED",
        "resource": "Document",
        "id": document_id,
    }
    assert await _count_jobs() == 1


@pytest.mark.asyncio
async def test_should_return_not_found_when_creating_a_job_for_an_unknown_document_id(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "document_id": f"doc_missing_{uuid4().hex[:12]}",
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "missing-document-upload.pdf",
        "data_id": "contract-job-missing-document",
    }

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Document not found"
    assert error["details"] == {
        "resource": "Document",
        "id": payload["document_id"],
    }
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_return_not_found_when_creating_a_job_for_an_archived_document(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_id = f"doc_archived_{uuid4().hex[:12]}"

    payload: dict[str, str] = {
        "document_id": document_id,
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "archived-document-upload.pdf",
        "data_id": "contract-job-archived-document",
    }

    async with developer_api_client_factory() as api_client:
        await _insert_document(document_id=document_id, status="archived")
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Document not found"
    assert error["details"] == {
        "resource": "Document",
        "id": document_id,
    }
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_create_a_waiting_file_job_for_a_url_source_and_enqueue_the_upload_worker(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "url",
        "source_url": "https://example.com/contracts/knowhere-upload",
        "data_id": "contract-job-url-upload",
    }
    requested_urls: list[str] = []
    scheduled_tasks: list[dict[str, object]] = []

    class _FakeHeadResponse:
        def __init__(self, content_type: str, status_code: int = 200) -> None:
            self.headers: dict[str, str] = {"content-type": content_type}
            self.status_code = status_code

    class _FakeAsyncHttpClient:
        async def head(
            self,
            url: str,
            *,
            follow_redirects: bool = True,
        ) -> _FakeHeadResponse:
            requested_urls.append(url)
            assert follow_redirects is False
            return _FakeHeadResponse("application/pdf")

    class _FakeCeleryTask:
        def __init__(self, task_name: str) -> None:
            self._task_name = task_name

        def apply_async(
            self,
            *,
            args: list[object],
            kwargs: dict[str, object],
        ) -> None:
            scheduled_tasks.append(
                {
                    "task_name": self._task_name,
                    "args": args,
                    "kwargs": kwargs,
                }
            )

    class _FakeCeleryApp:
        def __init__(self) -> None:
            from types import SimpleNamespace

            self.conf = SimpleNamespace(task_routes={})

        def signature(self, task_name: str) -> _FakeCeleryTask:
            return _FakeCeleryTask(task_name)

    import shared.core.celery_app as celery_app_module
    import shared.utils.http_clients as http_clients_module

    monkeypatch.setattr(
        http_clients_module,
        "get_async_client",
        lambda: _FakeAsyncHttpClient(),
    )
    monkeypatch.setattr(
        celery_app_module,
        "get_celery_app",
        lambda: _FakeCeleryApp(),
    )

    async with developer_api_client_factory() as api_client:
        from shared.services.redis import JobInfoRedisService, JobMetadataService
        from shared.services.redis.redis_service_factory import RedisServiceFactory

        response = await api_client.post("/api/v1/jobs", json=payload)

        assert response.status_code == 200

        response_json: dict[str, object] = response.json()
        job_id = cast(str, response_json["job_id"])

        assert requested_urls == [payload["source_url"], payload["source_url"]]
        assert response_json["status"] == "waiting-file"
        assert response_json["source_type"] == "url"
        assert response_json["namespace"] == payload["namespace"]
        assert response_json["data_id"] == payload["data_id"]
        assert response_json["document_id"] is None
        assert response_json["upload_url"] is None
        assert response_json["upload_headers"] is None
        assert response_json["expires_in"] is None

        job_row = await _load_job_record(job_id)
        job_metadata = cast(dict[str, object], job_row["job_metadata"])
        persisted_document_id = cast(str, job_metadata["document_id"])
        original_request = cast(dict[str, object], job_metadata["original_request"])

        assert job_row["user_id"] == "local-dev-user"
        assert job_row["job_type"] == "kb_management"
        assert job_row["status"] == "waiting-file"
        assert job_row["source_type"] == "url"
        assert job_row["s3_key"] == f"uploads/{job_id}.pdf"
        assert job_row["webhook_enabled"] is False
        assert persisted_document_id.startswith("doc_")
        assert job_metadata["namespace"] == payload["namespace"]
        assert job_metadata["source_type"] == "url"
        assert job_metadata["source_file_name"] == "knowhere-upload.pdf"
        assert job_metadata["source_url"] == payload["source_url"]
        assert job_metadata["data_id"] == payload["data_id"]
        assert original_request["source_type"] == payload["source_type"]
        assert original_request["source_url"] == payload["source_url"]

        redis_service = RedisServiceFactory.get_service()
        metadata_service = JobMetadataService(redis_service)
        job_info_service = JobInfoRedisService(redis_service)

        cached_metadata = await metadata_service.get_metadata(job_id)
        cached_job_info = await job_info_service.get_job_info(job_id)

        assert cached_metadata is not None
        assert cached_job_info is not None
        assert cached_metadata["document_id"] == persisted_document_id
        assert cached_metadata["namespace"] == payload["namespace"]
        assert cached_metadata["source_type"] == "url"
        assert cached_metadata["source_file_name"] == "knowhere-upload.pdf"
        assert cached_metadata["source_url"] == payload["source_url"]
        assert cached_job_info["job_id"] == job_id
        assert cached_job_info["user_id"] == "local-dev-user"
        assert cached_job_info["job_type"] == "kb_management"
        assert cached_job_info["source_type"] == "url"
        assert cached_job_info["s3_key"] == f"uploads/{job_id}.pdf"
        assert cached_job_info["webhook_enabled"] is False
        assert scheduled_tasks == [
            {
                "task_name": "app.core.tasks.kb_tasks.upload_url_file_task",
                "args": [job_id, payload["source_url"], "local-dev-user"],
                "kwargs": {"job_type": "kb_management"},
            }
        ]


@pytest.mark.asyncio
async def test_should_accept_an_http_webhook_url_when_creating_a_file_job_in_production(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    webhook_url = "http://hooks.example.test/notify"
    payload: dict[str, object] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-upload.pdf",
        "data_id": "contract-job-http-webhook",
        "webhook": {"url": webhook_url},
    }

    def resolve_public_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_public_address)

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 200
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    job_id = cast(str, response_json["job_id"])

    assert response_json["status"] == "waiting-file"
    assert response_json["source_type"] == "file"

    job_row = await _load_job_record(job_id)
    assert job_row["webhook_enabled"] is True
    assert job_row["webhook_url"] == webhook_url


@pytest.mark.asyncio
async def test_should_accept_a_private_url_source_when_creating_a_url_job_in_local_development(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    source_url = "http://127.0.0.1/contracts/local-private.pdf"
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "url",
        "source_url": source_url,
        "data_id": "contract-job-url-local-private-host",
    }
    scheduled_tasks: list[dict[str, object]] = []

    class _FakeCeleryTask:
        def __init__(self, task_name: str) -> None:
            self._task_name = task_name

        def apply_async(
            self,
            *,
            args: list[object],
            kwargs: dict[str, object],
        ) -> None:
            scheduled_tasks.append(
                {
                    "task_name": self._task_name,
                    "args": args,
                    "kwargs": kwargs,
                }
            )

    class _FakeCeleryApp:
        def __init__(self) -> None:
            from types import SimpleNamespace

            self.conf = SimpleNamespace(task_routes={})

        def signature(self, task_name: str) -> _FakeCeleryTask:
            return _FakeCeleryTask(task_name)

    def resolve_private_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    import shared.core.celery_app as celery_app_module
    monkeypatch.setattr(socket, "getaddrinfo", resolve_private_address)
    monkeypatch.setattr(
        celery_app_module,
        "get_celery_app",
        lambda: _FakeCeleryApp(),
    )

    async with developer_api_client_factory() as api_client:
        import shared.core.config as shared_config_module

        monkeypatch.setattr(shared_config_module.app_config, "ENVIRONMENT", "local")
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 200
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    job_id = cast(str, response_json["job_id"])

    assert response_json["status"] == "waiting-file"
    assert response_json["source_type"] == "url"

    job_row = await _load_job_record(job_id)
    job_metadata = cast(dict[str, object], job_row["job_metadata"])

    assert job_row["source_type"] == "url"
    assert job_metadata["source_url"] == source_url
    assert scheduled_tasks == [
        {
            "task_name": "app.core.tasks.kb_tasks.upload_url_file_task",
            "args": [job_id, source_url, "local-dev-user"],
            "kwargs": {"job_type": "kb_management"},
        }
    ]


@pytest.mark.asyncio
async def test_should_reject_url_source_when_url_resolves_to_private_network(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "url",
        "source_url": "https://files.example.test/contracts/private.pdf",
        "data_id": "contract-job-url-private-host",
    }

    def resolve_private_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_private_address)

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"] == "Invalid URL"
    assert violations == [
        {
            "field": "source_url",
            "description": "URL host is not allowed",
        }
    ]
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_reject_a_url_source_when_file_type_detection_redirects_to_a_private_host(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "url",
        "source_url": "https://example.com/contracts/knowhere-upload",
        "data_id": "contract-job-url-private-redirect",
    }
    requested_urls: list[str] = []

    class _FakeHeadResponse:
        status_code = 302
        headers: dict[str, str] = {
            "location": "http://127.0.0.1/internal-metadata.pdf",
        }

    class _FakeAsyncHttpClient:
        async def head(
            self,
            url: str,
            *,
            follow_redirects: bool = True,
        ) -> _FakeHeadResponse:
            requested_urls.append(url)
            assert follow_redirects is False
            return _FakeHeadResponse()

    import shared.utils.http_clients as http_clients_module
    monkeypatch.setattr(
        http_clients_module,
        "get_async_client",
        lambda: _FakeAsyncHttpClient(),
    )

    async with developer_api_client_factory() as api_client:
        response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json: dict[str, object] = response.json()
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert requested_urls == [payload["source_url"]]
    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"] == "Invalid URL"
    assert violations == [
        {
            "field": "source_url",
            "description": "URL host is not allowed",
        }
    ]
    assert await _count_jobs() == 0


@pytest.mark.asyncio
async def test_should_confirm_upload_and_start_processing_for_a_waiting_file_job(
    monkeypatch: MonkeyPatch,
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "confirm-upload.pdf",
        "data_id": "contract-job-confirm-upload",
    }
    started_workflows: list[dict[str, object]] = []

    async def _fake_verify_s3_file_exists(
        self: object,
        s3_key: str,
        bucket: str | None = None,
    ) -> dict[str, object]:
        assert bucket is None
        return {"exists": True, "s3_key": s3_key}

    async def _fake_start_workflow_for_job(
        db: object,
        job_id: str,
        job_type: str,
        source_type: str,
        user_id: str,
        file_path: str | None = None,
        file_url: str | None = None,
    ) -> None:
        started_workflows.append(
            {
                "job_id": job_id,
                "job_type": job_type,
                "source_type": source_type,
                "user_id": user_id,
                "file_path": file_path,
                "file_url": file_url,
                "db_bound": db is not None,
            }
        )

    async with developer_api_client_factory() as api_client:
        import app.api.v1.routes.jobs as jobs_route_module
        import shared.services.storage.file_upload_service as file_upload_service_module

        monkeypatch.setattr(
            file_upload_service_module.FileUploadService,
            "verify_s3_file_exists",
            _fake_verify_s3_file_exists,
        )
        monkeypatch.setattr(
            jobs_route_module,
            "start_workflow_for_job",
            _fake_start_workflow_for_job,
        )

        create_response = await api_client.post("/api/v1/jobs", json=payload)
        assert create_response.status_code == 200

        create_response_json: dict[str, object] = create_response.json()
        job_id = cast(str, create_response_json["job_id"])

        confirm_response = await api_client.post(f"/api/v1/jobs/{job_id}/confirm-upload")

    assert confirm_response.status_code == 200
    assert confirm_response.headers["x-request-id"]
    assert confirm_response.json() == {
        "message": "File upload confirmed; processing started"
    }

    job_row = await _load_job_record(job_id)
    assert job_row["status"] == "pending"
    assert started_workflows == [
        {
            "job_id": job_id,
            "job_type": "kb_management",
            "source_type": "file",
            "user_id": "local-dev-user",
            "file_path": None,
            "file_url": None,
            "db_bound": True,
        }
    ]
