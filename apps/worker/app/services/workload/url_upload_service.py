from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.workload.url_upload_context import load_url_upload_context
from app.services.workload.url_upload_transfer import (
    assert_temp_file_within_size_limit,
    cleanup_temp_file,
    download_source_url_to_temp,
    resolve_supported_url_extension,
    upload_temp_file_to_source_storage,
    verify_source_upload,
)
from shared.services.jobs.lifecycle.service import get_sync_job_lifecycle_service
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory


def upload_url_file(
    job_id: str,
    source_url: str,
    user_id: str | None,
    job_type: str | None = None,
) -> dict[str, Any]:
    del user_id, job_type

    lifecycle_service = get_sync_job_lifecycle_service()
    redis_service = SyncRedisServiceFactory.get_service()
    upload_context = load_url_upload_context(job_id, redis_service)

    lifecycle_service.update_progress(
        job_id, progress=3, message="Validating URL file type..."
    )
    file_extension = resolve_supported_url_extension(source_url)

    lifecycle_service.update_progress(
        job_id, progress=10, message="Downloading file from URL..."
    )
    temp_file_path = download_source_url_to_temp(source_url)

    try:
        lifecycle_service.update_progress(
            job_id, progress=30, message="Validating file size..."
        )
        assert_temp_file_within_size_limit(
            temp_file_path=temp_file_path,
            file_extension=file_extension,
        )

        lifecycle_service.update_progress(
            job_id, progress=50, message="Uploading file to S3..."
        )
        upload_temp_file_to_source_storage(
            temp_file_path=temp_file_path,
            s3_key=upload_context.s3_key,
        )

    finally:
        cleanup_temp_file(temp_file_path)

    lifecycle_service.update_progress(
        job_id, progress=80, message="Verifying upload result..."
    )
    file_info = verify_source_upload(upload_context.s3_key)

    lifecycle_service.update_progress(
        job_id,
        progress=100,
        message="URL file upload complete, waiting for processing...",
    )
    logger.info(
        "URL file upload complete, waiting for S3 webhook: "
        f"{job_id} -> {upload_context.s3_key}"
    )

    return {
        "status": "success",
        "job_id": job_id,
        "s3_key": upload_context.s3_key,
        "file_size": file_info.get("size"),
    }
