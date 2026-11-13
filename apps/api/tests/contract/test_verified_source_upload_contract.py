from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.stub import Stubber
from httpx import AsyncClient, Response

from tests.support.contract_database import ContractDatabase

_ORIGINAL = b"exact original bytes"
_DIGEST = sha256(_ORIGINAL).digest()
_SOURCE_INTEGRITY = {"size_bytes": len(_ORIGINAL), "sha256": _DIGEST.hex()}


def _get_payload() -> dict[str, object]:
    return {
        "namespace": "verified-source-upload",
        "source_type": "file",
        "file_name": "original.pdf",
        "source_integrity": _SOURCE_INTEGRITY,
    }


def _enable_s3_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.core.config import settings

    monkeypatch.setattr(settings, "S3_TYPE", "s3")
    monkeypatch.setattr(settings, "S3_USE_SSL", True)


def _stub_uploaded_object(monkeypatch: pytest.MonkeyPatch) -> tuple[Stubber, str]:
    from shared.core.config import settings
    from shared.services.storage import job_file_storage
    from shared.services.storage.adapters.s3_adapter import S3StorageAdapter

    client = boto3.client(
        "s3",
        region_name="us-west-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    bucket: str = settings.S3_BUCKET_NAME
    adapter = S3StorageAdapter(client, bucket)
    monkeypatch.setattr(job_file_storage, "get_cached_storage_adapter", lambda: adapter)
    return Stubber(client), bucket


def _upload_event(bucket: str, key: str) -> dict[str, object]:
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "awsRegion": "us-west-1",
                "eventTime": datetime.now(timezone.utc).isoformat(),
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": len(_ORIGINAL)},
                },
            }
        ],
    }


def _check_signed_upload(result: dict[str, object]) -> None:
    assert result["upload_headers"] == {
        "Content-Type": "application/pdf",
        "Content-Length": str(len(_ORIGINAL)),
        "If-None-Match": "*",
        "x-amz-checksum-sha256": b64encode(_DIGEST).decode("ascii"),
    }
    url = urlsplit(str(result["upload_url"]))
    assert url.scheme == "https"
    query = parse_qs(url.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-SignedHeaders"] == [
        "content-length;content-type;host;if-none-match;x-amz-checksum-sha256"
    ]


@pytest.mark.asyncio
async def test_signs_exact_original_and_preserves_the_constraints_on_renewal(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        _enable_s3_signing(monkeypatch)
        created = await api_client.post("/api/v2/jobs", json=_get_payload())
        assert created.status_code == 200
        job = created.json()
        assert job["status"] == "waiting-file"
        _check_signed_upload(job)

        stored = await ContractDatabase.fetch_job(job["job_id"])
        assert (
            stored is not None
            and stored["job_metadata"]["source_integrity"] == _SOURCE_INTEGRITY
        )
        status = (await api_client.get(f"/api/v2/jobs/{job['job_id']}")).json()
        assert status["job_id"] == job["job_id"] and status["status"] == "waiting-file"

        renewed = await api_client.post(f"/api/v2/jobs/{job['job_id']}/upload-url")
        assert renewed.status_code == 200
        _check_signed_upload(renewed.json())
        assert (
            urlsplit(renewed.json()["upload_url"]).path
            == urlsplit(job["upload_url"]).path
        )
        assert 0 < renewed.json()["expires_in"] <= job["expires_in"]


@pytest.mark.asyncio
async def test_integrity_requires_valid_file_metadata_and_supported_storage(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        for payload in (
            {
                **_get_payload(),
                "source_type": "url",
                "source_url": "https://example.com/original.pdf",
            },
            {
                **_get_payload(),
                "source_integrity": {**_SOURCE_INTEGRITY, "size_bytes": True},
            },
            {
                **_get_payload(),
                "source_integrity": {**_SOURCE_INTEGRITY, "sha256": "invalid"},
            },
            {
                **_get_payload(),
                "source_integrity": {**_SOURCE_INTEGRITY, "size_bytes": 10**10},
            },
        ):
            assert (
                await api_client.post("/api/v2/jobs", json=payload)
            ).status_code == 400

        unsupported = await api_client.post("/api/v2/jobs", json=_get_payload())
        assert unsupported.status_code == 503
        assert (
            await ContractDatabase.fetch_one("SELECT job_id FROM jobs LIMIT 1") is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["manual", "s3"])
async def test_unavailable_or_missing_upload_remains_waiting_until_verified(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    publication: list[str] = []

    async def _publish(self: object, *, job_id: str, user_id: str) -> str:
        publication.append(job_id)
        return "contract-task"

    async with developer_api_client_factory() as api_client:
        from app.services.document_ingestion.worker_dispatcher import (
            DocumentIngestionWorkerDispatcher,
        )

        _enable_s3_signing(monkeypatch)
        stubber, bucket = _stub_uploaded_object(monkeypatch)
        monkeypatch.setattr(
            DocumentIngestionWorkerDispatcher, "start_uploaded_file_parse", _publish
        )
        created = await api_client.post("/api/v2/jobs", json=_get_payload())
        assert created.status_code == 200
        job_id: str = created.json()["job_id"]
        key = f"uploads/{job_id}.pdf"
        confirm_url = f"/api/v2/jobs/{job_id}/confirm-upload"
        event = _upload_event(bucket, key)
        expected_head = {"Bucket": bucket, "Key": key, "ChecksumMode": "ENABLED"}
        for code in (403, 404):
            stubber.add_client_error(
                "head_object",
                service_error_code=str(code),
                http_status_code=code,
                expected_params=expected_head,
            )
        stubber.add_response(
            "head_object",
            {
                "ContentLength": len(_ORIGINAL),
                "ChecksumSHA256": b64encode(_DIGEST).decode("ascii"),
                "LastModified": datetime.now(timezone.utc),
            },
            expected_head,
        )

        async def _confirm() -> Response:
            if trigger == "manual":
                return await api_client.post(confirm_url)
            return await api_client.post("/api/v1/internal/s3-events", json=event)

        with stubber:
            for expected_status in (503, 400):
                response = await _confirm()
                if trigger == "manual":
                    assert response.status_code == expected_status
                    error = response.json()["error"]
                    if expected_status == 503:
                        assert error["code"] == "UNAVAILABLE"
                    else:
                        assert error["details"]["violations"][0]["field"] == "file"
                else:
                    assert response.status_code == 200
                assert not publication
                stored = await ContractDatabase.fetch_job(job_id)
                assert stored is not None and stored["status"] == "waiting-file"
            assert (await _confirm()).status_code == 200
            assert (await _confirm()).status_code == 200
            stubber.assert_no_pending_responses()
        assert publication == [job_id]
        assert (await api_client.get(f"/api/v2/jobs/{job_id}")).json()[
            "status"
        ] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["manual", "s3"])
@pytest.mark.parametrize(
    "metadata",
    [
        {
            "ContentLength": len(_ORIGINAL),
            "ChecksumSHA256": b64encode(bytes(32)).decode("ascii"),
        },
        {
            "ContentLength": len(_ORIGINAL) + 1,
            "ChecksumSHA256": b64encode(_DIGEST).decode("ascii"),
        },
        {"ContentLength": len(_ORIGINAL)},
    ],
    ids=["wrong-checksum", "wrong-length", "missing-checksum"],
)
async def test_invalid_stored_original_immediately_fails_the_waiting_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    metadata: dict[str, object],
) -> None:
    async with developer_api_client_factory() as api_client:
        _enable_s3_signing(monkeypatch)
        stubber, bucket = _stub_uploaded_object(monkeypatch)
        created = await api_client.post("/api/v2/jobs", json=_get_payload())
        assert created.status_code == 200
        job_id: str = created.json()["job_id"]
        key = f"uploads/{job_id}.pdf"
        confirm_url = f"/api/v2/jobs/{job_id}/confirm-upload"
        event = _upload_event(bucket, key)
        stubber.add_response(
            "head_object",
            {**metadata, "LastModified": datetime.now(timezone.utc)},
            {"Bucket": bucket, "Key": key, "ChecksumMode": "ENABLED"},
        )

        async def _confirm() -> Response:
            if trigger == "manual":
                return await api_client.post(confirm_url)
            return await api_client.post("/api/v1/internal/s3-events", json=event)

        with stubber:
            rejected = await _confirm()
            if trigger == "manual":
                assert rejected.status_code == 400
                error = rejected.json()["error"]
                assert error["code"] == "INVALID_ARGUMENT"
                assert error["details"]["violations"][0]["field"] == "source_integrity"
            else:
                assert rejected.status_code == 200
            status = (await api_client.get(f"/api/v2/jobs/{job_id}")).json()
            assert status["status"] == "failed"
            assert status["error"]["code"] == "UPLOAD_INTEGRITY_MISMATCH"
            assert (await _confirm()).status_code == 200
            renewed = await api_client.post(f"/api/v2/jobs/{job_id}/upload-url")
            assert renewed.status_code == 409
            stubber.assert_no_pending_responses()

        assert (
            await ContractDatabase.fetch_one(
                "SELECT job_id FROM upload_parse_dispatches WHERE job_id = :job_id",
                {"job_id": job_id},
            )
            is None
        )
        assert await ContractDatabase.fetch_all(
            """
            SELECT from_state, to_state,
                   transition_metadata ->> 'error_code' AS error_code
            FROM job_state_audit_logs
            WHERE job_id = :job_id AND to_state = 'failed'
            """,
            {"job_id": job_id},
        ) == [
            {
                "from_state": "waiting-file",
                "to_state": "failed",
                "error_code": "UPLOAD_INTEGRITY_MISMATCH",
            }
        ]


@pytest.mark.asyncio
async def test_expired_verified_upload_cannot_be_renewed_or_manually_started(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with developer_api_client_factory() as api_client:
        _enable_s3_signing(monkeypatch)
        stubber, bucket = _stub_uploaded_object(monkeypatch)
        created = await api_client.post("/api/v2/jobs", json=_get_payload())
        assert created.status_code == 200
        job_id: str = created.json()["job_id"]
        stubber.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={
                "Bucket": bucket,
                "Key": f"uploads/{job_id}.pdf",
                "ChecksumMode": "ENABLED",
            },
        )
        await ContractDatabase.execute(
            "UPDATE jobs SET created_at = now() - interval '3 hours' WHERE job_id = :job_id",
            {"job_id": job_id},
        )
        with stubber:
            assert (
                await api_client.post(f"/api/v2/jobs/{job_id}/upload-url")
            ).status_code == 409
            rejected = await api_client.post(f"/api/v2/jobs/{job_id}/confirm-upload")
            stubber.assert_no_pending_responses()
        assert rejected.status_code == 409
        assert (await api_client.get(f"/api/v2/jobs/{job_id}")).json()[
            "status"
        ] == "failed"
        stored = await ContractDatabase.fetch_job(job_id)
        assert stored is not None and stored["error_code"] == "UPLOAD_EXPIRED"
