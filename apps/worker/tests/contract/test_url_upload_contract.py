from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job, insert_contract_user


def _load_upload_task_modules() -> tuple[Any, Any, Engine, Any, Any]:
    import app.core.tasks.kb_tasks as kb_tasks
    import app.services.workload.url_upload_service as url_upload_service
    from shared.core.database_sync import get_sync_engine
    from shared.services.redis.redis_sync_service import (
        SyncJobInfoRedisService,
        SyncRedisServiceFactory,
    )

    return (
        kb_tasks,
        url_upload_service,
        get_sync_engine(),
        SyncJobInfoRedisService,
        SyncRedisServiceFactory,
    )


def test_should_upload_a_url_job_to_the_expected_storage_key_and_publish_progress(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        kb_tasks,
        url_upload_service,
        engine,
        sync_job_info_service_cls,
        sync_redis_service_factory,
    ) = _load_upload_task_modules()
    from shared.services.storage.job_file_storage import JobFileStorage

    user_id = f"worker-user-{uuid4().hex[:12]}"
    job_id = f"job_url_upload_{uuid4().hex[:12]}"
    source_url = "https://example.test/files/contract-source.pdf"
    s3_key = f"uploads/{job_id}.pdf"
    downloaded_path = tmp_path / "downloaded-contract-source.pdf"
    uploaded_calls: list[tuple[str, str, str]] = []

    def resolve_public_address(
        host: str,
        port: int | None,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(
        JobFileStorage,
        "download_file_from_url",
        lambda self, _source_url, *, temp_dir=None: str(downloaded_path),
    )
    monkeypatch.setattr(
        JobFileStorage,
        "upload_source_file",
        lambda self, local_path, storage_key: uploaded_calls.append(
            (local_path, storage_key, self.uploads_bucket)
        ),
    )
    monkeypatch.setattr(
        JobFileStorage,
        "verify_upload_exists",
        lambda self, storage_key: {"exists": storage_key == s3_key, "size": 3},
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolve_public_address)

    downloaded_path.write_bytes(b"pdf")

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="waiting-file",
            source_type="url",
            s3_key=s3_key,
            job_metadata={
                "namespace": "worker-contract",
                "source_type": "url",
                "source_url": source_url,
                "source_file_name": "contract-source.pdf",
            },
        )

    redis_service = sync_redis_service_factory.get_service()
    sync_job_info_service = sync_job_info_service_cls(redis_service)
    sync_job_info_service.save_job_info(
        job_id,
        {
            "job_id": job_id,
            "s3_key": s3_key,
            "user_id": user_id,
            "webhook_enabled": False,
            "job_type": "kb_management",
            "source_type": "url",
        },
    )

    result = kb_tasks.upload_url_file_task.run(
        job_id,
        source_url,
        user_id,
        "kb_management",
    )

    assert result == {
        "status": "success",
        "job_id": job_id,
        "s3_key": s3_key,
        "file_size": 3,
    }
    assert uploaded_calls == [
        (str(downloaded_path), s3_key, url_upload_service.settings.S3_BUCKET_NAME),
    ]
    assert os.path.exists(downloaded_path) is False

    progress = redis_service.hgetall(f"task:{job_id}:progress")
    assert progress["progress"] == 100
    assert progress["message"] == "URL file upload complete, waiting for processing..."
    assert progress["timestamp"]

    with engine.begin() as connection:
        job_row = (
            connection.execute(
                text(
                    """
                    SELECT status, source_type, s3_key
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )

    assert job_row["status"] == "waiting-file"
    assert job_row["source_type"] == "url"
    assert job_row["s3_key"] == s3_key
