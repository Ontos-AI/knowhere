from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import boto3
import pytest
from botocore.stub import Stubber
from httpx import AsyncClient

from tests.support.contract_database import ContractDatabase

_ORIGINAL = b"original upload bytes"
_DIGEST = sha256(_ORIGINAL).digest()
_CREATE_JOB = {
    "namespace": "guarded-deadline",
    "source_type": "file",
    "file_name": "original.pdf",
    "source_integrity": {"size_bytes": len(_ORIGINAL), "sha256": _DIGEST.hex()},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["notification", "confirm"])
@pytest.mark.parametrize(
    ("upload_delay", "is_checksum_valid", "expected_error", "confirm_status"),
    [
        (timedelta(hours=1), True, None, 200),
        (timedelta(hours=2, minutes=30), True, "UPLOAD_EXPIRED", 409),
        (timedelta(hours=1), False, "UPLOAD_INTEGRITY_MISMATCH", 400),
    ],
    ids=["on-time-original", "late-original", "on-time-mismatch"],
)
async def test_guarded_upload_uses_storage_evidence_after_deadline(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    upload_delay: timedelta,
    is_checksum_valid: bool,
    expected_error: str | None,
    confirm_status: int,
) -> None:
    async with developer_api_client_factory() as client:
        from app.services.document_ingestion.worker_dispatcher import (
            DocumentIngestionWorkerDispatcher,
        )
        from shared.core.config import settings
        from shared.services.storage import job_file_storage
        from shared.services.storage.adapters.s3_adapter import S3StorageAdapter

        monkeypatch.setattr(settings, "S3_TYPE", "s3")
        monkeypatch.setattr(settings, "S3_USE_SSL", True)
        s3 = boto3.client(
            "s3",
            region_name="us-west-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        bucket = settings.S3_BUCKET_NAME
        adapter = S3StorageAdapter(s3, bucket)
        monkeypatch.setattr(
            job_file_storage, "get_cached_storage_adapter", lambda: adapter
        )
        published_jobs: list[str] = []

        async def publish(self: object, *, job_id: str, user_id: str) -> str:
            published_jobs.append(job_id)
            return "test-task"

        monkeypatch.setattr(
            DocumentIngestionWorkerDispatcher, "start_uploaded_file_parse", publish
        )

        created = await client.post("/api/v2/jobs", json=_CREATE_JOB)
        assert created.status_code == 200
        job_id: str = created.json()["job_id"]
        key = f"uploads/{job_id}.pdf"
        created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        uploaded_at = created_at + upload_delay
        await ContractDatabase.execute(
            "UPDATE jobs SET created_at = :created_at, updated_at = :created_at WHERE job_id = :job_id",
            {"job_id": job_id, "created_at": created_at.replace(tzinfo=None)},
        )
        event = {
            "Records": [
                {
                    "eventVersion": "2.1",
                    "eventSource": "aws:s3",
                    "awsRegion": "us-west-1",
                    # A notification timestamp alone cannot admit an object whose HEAD is late.
                    "eventTime": (created_at + timedelta(hours=1)).isoformat(),
                    "eventName": "ObjectCreated:Put",
                    "s3": {"bucket": {"name": bucket}, "object": {"key": key}},
                }
            ]
        }

        with Stubber(s3) as stubber:
            stubber.add_response(
                "head_object",
                {
                    "ContentLength": len(_ORIGINAL),
                    "ChecksumSHA256": b64encode(
                        _DIGEST if is_checksum_valid else bytes(32)
                    ).decode("ascii"),
                    "LastModified": uploaded_at,
                },
                {"Bucket": bucket, "Key": key, "ChecksumMode": "ENABLED"},
            )
            if trigger == "notification":
                response = await client.post("/api/v1/internal/s3-events", json=event)
            else:
                response = await client.post(f"/api/v2/jobs/{job_id}/confirm-upload")
            job = await ContractDatabase.fetch_job(job_id)
            assert job is not None
            assert job["error_code"] == expected_error
            if expected_error is None:
                assert job["status"] == "pending"
                assert published_jobs == [job_id]
            else:
                assert job["status"] == "failed"
                assert published_jobs == []
            assert response.status_code == (
                200 if trigger == "notification" else confirm_status
            )
            stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [0, -1])
async def test_nonpositive_legacy_expiration_does_not_break_guarded_signing(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
    setting: int,
) -> None:
    async with developer_api_client_factory() as client:
        from shared.core.config import settings

        monkeypatch.setattr(settings, "S3_TYPE", "s3")
        monkeypatch.setattr(settings, "S3_USE_SSL", True)
        monkeypatch.setattr(settings, "JOB_WAITING_EXPIRE_SECONDS", setting)
        created = await client.post("/api/v2/jobs", json=_CREATE_JOB)
        assert created.status_code == 200
        job = created.json()
        assert job["status"] == "waiting-file"
        assert 0 < job["expires_in"] <= 7200
        renewed = await client.post(f"/api/v2/jobs/{job['job_id']}/upload-url")
        assert renewed.status_code == 200
        assert 0 < renewed.json()["expires_in"] <= 7200
