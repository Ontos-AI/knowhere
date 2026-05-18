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
    import app.core.tasks.document_ingestion_tasks as document_ingestion_tasks
    import app.services.workload.url_upload_service as url_upload_service
    from shared.core.database_sync import get_sync_engine
    from shared.services.redis.redis_sync_service import (
        SyncJobInfoRedisService,
        SyncRedisServiceFactory,
    )

    return (
        document_ingestion_tasks,
        url_upload_service,
        get_sync_engine(),
        SyncJobInfoRedisService,
        SyncRedisServiceFactory,
    )


def _bind_upload_task_to_current_module(
    monkeypatch: MonkeyPatch,
    *,
    document_ingestion_tasks: Any,
) -> None:
    monkeypatch.setitem(
        document_ingestion_tasks.upload_url_file_task._orig_run.__globals__,
        "_upload_url_file",
        document_ingestion_tasks._upload_url_file,
    )
    monkeypatch.setattr(
        document_ingestion_tasks.upload_url_file_task,
        "__trace__",
        None,
        raising=False,
    )


def test_should_upload_a_url_job_to_the_expected_storage_key_and_publish_progress(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        document_ingestion_tasks,
        url_upload_service,
        engine,
        sync_job_info_service_cls,
        sync_redis_service_factory,
    ) = _load_upload_task_modules()
    from shared.core.config import settings

    user_id = f"worker-user-{uuid4().hex[:12]}"
    job_id = f"job_url_upload_{uuid4().hex[:12]}"
    source_url = "https://example.test/files/contract-source.pdf"
    s3_key = f"uploads/{job_id}.pdf"
    downloaded_path = tmp_path / "downloaded-contract-source.pdf"
    uploaded_calls: list[tuple[str, str, str]] = []

    def resolve_public_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(
        url_upload_service,
        "download_source_url_to_temp",
        lambda _source_url: str(downloaded_path),
    )
    monkeypatch.setitem(
        document_ingestion_tasks.upload_url_file.__globals__,
        "download_source_url_to_temp",
        lambda _source_url: str(downloaded_path),
    )
    monkeypatch.setattr(
        url_upload_service,
        "upload_temp_file_to_source_storage",
        lambda *, temp_file_path, s3_key: uploaded_calls.append(
            (temp_file_path, s3_key, settings.S3_BUCKET_NAME)
        ),
    )
    monkeypatch.setitem(
        document_ingestion_tasks.upload_url_file.__globals__,
        "upload_temp_file_to_source_storage",
        lambda *, temp_file_path, s3_key: uploaded_calls.append(
            (temp_file_path, s3_key, settings.S3_BUCKET_NAME)
        ),
    )
    monkeypatch.setattr(
        url_upload_service,
        "verify_source_upload",
        lambda storage_key: {"exists": storage_key == s3_key, "size": 3},
    )
    monkeypatch.setitem(
        document_ingestion_tasks.upload_url_file.__globals__,
        "verify_source_upload",
        lambda storage_key: {"exists": storage_key == s3_key, "size": 3},
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolve_public_address)
    _bind_upload_task_to_current_module(
        monkeypatch,
        document_ingestion_tasks=document_ingestion_tasks,
    )

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
            "job_type": "document_ingestion",
            "source_type": "url",
        },
    )

    result = document_ingestion_tasks.upload_url_file_task.run(
        job_id,
        source_url,
        user_id,
        "document_ingestion",
    )

    assert result == {
        "status": "success",
        "job_id": job_id,
        "s3_key": s3_key,
        "file_size": 3,
    }
    assert uploaded_calls == [
        (str(downloaded_path), s3_key, settings.S3_BUCKET_NAME),
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
