from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job, insert_contract_user


def _build_file_job_metadata() -> dict[str, str]:
    return {
        "document_id": f"doc_{uuid4().hex[:12]}",
        "namespace": "worker-contract",
        "source_type": "file",
    }


def _load_worker_modules() -> tuple[Any, Engine]:
    import app.core.tasks.stale_job_sweeper as stale_job_sweeper
    from shared.core.database_sync import get_sync_engine

    return stale_job_sweeper, get_sync_engine()


def test_should_expire_stale_jobs_and_persist_failure_state(
    worker_contract_environment: None,
) -> None:
    stale_job_id = f"job_stale_{uuid4().hex[:12]}"
    fresh_job_id = f"job_fresh_{uuid4().hex[:12]}"
    user_id = f"worker-user-{uuid4().hex[:12]}"

    stale_job_sweeper, engine = _load_worker_modules()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_updated_at = now - timedelta(days=1)

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=stale_job_id,
            user_id=user_id,
            status="running",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
            created_at=stale_updated_at - timedelta(minutes=5),
            updated_at=stale_updated_at,
        )
        insert_contract_job(
            connection,
            job_id=fresh_job_id,
            user_id=user_id,
            status="waiting-file",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
            created_at=now - timedelta(minutes=5),
            updated_at=now,
        )

    result = stale_job_sweeper.expire_stale_jobs()

    assert result == {"status": "success", "expired": 1, "skipped": 0, "indexing": {"expired": 0, "deferred": 0}}

    with engine.begin() as connection:
        stale_job_row = (
            connection.execute(
                text(
                    """
                    SELECT status, error_code, error_message
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": stale_job_id},
            )
            .mappings()
            .one()
        )
        fresh_job_row = (
            connection.execute(
                text(
                    """
                    SELECT status
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": fresh_job_id},
            )
            .mappings()
            .one()
        )
        audit_log_row = (
            connection.execute(
                text(
                    """
                    SELECT from_state, to_state, transition_reason, transition_metadata
                    FROM job_state_audit_logs
                    WHERE job_id = :job_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"job_id": stale_job_id},
            )
            .mappings()
            .one()
        )

    audit_metadata = dict(audit_log_row["transition_metadata"])

    assert stale_job_row["status"] == "failed"
    assert stale_job_row["error_code"] == stale_job_sweeper.JOB_EXPIRED_ERROR_CODE
    assert stale_job_row["error_message"] == stale_job_sweeper.JOB_EXPIRED_ERROR_MESSAGE
    assert fresh_job_row["status"] == "waiting-file"
    assert audit_log_row["from_state"] == "running"
    assert audit_log_row["to_state"] == "failed"
    assert audit_log_row["transition_reason"] == "mark_failed"
    assert audit_metadata["sweeper"] is True
    assert audit_metadata["stale_status"] == "running"
    assert audit_metadata["error_code"] == stale_job_sweeper.JOB_EXPIRED_ERROR_CODE


def test_should_skip_duplicate_beat_firing_with_the_real_periodic_redis_lock(
    worker_contract_environment: None,
) -> None:
    stale_job_sweeper, _ = _load_worker_modules()

    first_result = stale_job_sweeper.expire_stale_jobs()
    second_result = stale_job_sweeper.expire_stale_jobs()

    assert first_result == {"status": "success", "expired": 0, "skipped": 0, "indexing": {"expired": 0, "deferred": 0}}
    assert second_result == {
        "status": "skipped",
        "reason": "duplicate Beat firing",
    }


@pytest.mark.parametrize(
    ("storage_result", "expected_status"),
    [
        ("matched", "pending"),
        ("unavailable", "waiting-file"),
        ("connection-error", "waiting-file"),
        ("concurrent-confirmation", "pending"),
    ],
)
def test_sweeper_checks_storage_before_expiring_guarded_uploads(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    storage_result: str,
    expected_status: str,
) -> None:
    from shared.core.config import settings
    from shared.core.database_sync import get_sync_db_context
    from shared.core.state_machine.service_sync import SyncStateMachineService
    from shared.services.jobs.upload_parse_dispatch import UploadParseOutbox
    from shared.services.storage import job_file_storage
    from shared.services.storage.adapters.s3_adapter import S3StorageAdapter

    stale_job_sweeper, engine = _load_worker_modules()
    user_id = f"worker-user-{uuid4().hex[:12]}"
    job_id = f"job_guarded_{uuid4().hex[:12]}"
    key = f"uploads/{job_id}.pdf"
    legacy_job_id = f"job_legacy_{uuid4().hex[:12]}"
    original = b"original upload bytes"
    digest = sha256(original).digest()
    created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    uploaded_at = created_at + timedelta(hours=1)

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="waiting-file",
            s3_key=key,
            job_metadata={
                **_build_file_job_metadata(),
                "source_integrity": {"size_bytes": len(original), "sha256": digest.hex()},
            },
            created_at=created_at.replace(tzinfo=None),
        )
        insert_contract_job(
            connection,
            job_id=legacy_job_id,
            user_id=user_id,
            status="waiting-file",
            job_metadata=_build_file_job_metadata(),
            created_at=created_at.replace(tzinfo=None),
        )

    s3 = boto3.client(
        "s3", region_name="us-west-1",
        aws_access_key_id="test", aws_secret_access_key="test",
    )
    bucket = settings.S3_BUCKET_NAME
    adapter = S3StorageAdapter(s3, bucket)
    monkeypatch.setattr(job_file_storage, "get_cached_storage_adapter", lambda: adapter)
    if storage_result == "concurrent-confirmation":
        # Another transaction can confirm while the worker is reading storage.
        # The worker must neither fail the fresh pending job nor stage twice.
        def confirm_during_head(**_: object) -> None:
            with get_sync_db_context() as db:
                assert SyncStateMachineService().transition_outcome(
                    db, job_id, "pending", "manual_upload_completed"
                ).succeeded
                UploadParseOutbox.stage(db, job_id=job_id, user_id=user_id)

        s3.meta.events.register("after-call.s3.HeadObject", confirm_during_head)
    if storage_result == "connection-error":
        def raise_connection_error(**_: object) -> None:
            raise EndpointConnectionError(endpoint_url="https://s3.example.invalid")

        s3.meta.events.register("before-parameter-build.s3.HeadObject", raise_connection_error)

    with Stubber(s3) as stubber:
        params = {"Bucket": bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if storage_result in {"matched", "concurrent-confirmation"}:
            stubber.add_response(
                "head_object",
                {
                    "ContentLength": len(original),
                    "ChecksumSHA256": b64encode(digest).decode("ascii"),
                    "LastModified": uploaded_at,
                },
                params,
            )
        elif storage_result == "unavailable":
            stubber.add_client_error(
                "head_object", "ServiceUnavailable", http_status_code=503,
                expected_params=params,
            )
        result = stale_job_sweeper.expire_stale_jobs()
        with engine.begin() as connection:
            job = connection.execute(
                text("SELECT status, error_code FROM jobs WHERE job_id = :job_id"),
                {"job_id": job_id},
            ).mappings().one()
            dispatch = connection.execute(
                text("SELECT job_id FROM upload_parse_dispatches WHERE job_id = :job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
            legacy_job = connection.execute(
                text("SELECT status, error_code FROM jobs WHERE job_id = :job_id"),
                {"job_id": legacy_job_id},
            ).mappings().one()
        assert (job["status"], job["error_code"]) == (expected_status, None)
        assert (legacy_job["status"], legacy_job["error_code"]) == ("failed", "JOB_EXPIRED")
        assert result["expired"] == 1
        assert dispatch == (job_id if expected_status == "pending" else None)
        if expected_status == "waiting-file":
            with engine.begin() as connection:
                last_attempt = connection.execute(
                    text("SELECT updated_at FROM jobs WHERE job_id = :job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            assert last_attempt > created_at.replace(tzinfo=None)
        stubber.assert_no_pending_responses()
