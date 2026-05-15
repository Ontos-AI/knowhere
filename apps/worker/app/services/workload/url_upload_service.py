from __future__ import annotations

import os
from typing import Any

from app.services.storage.sync_storage_service import (
    download_file_from_url,
    upload_to_s3,
    verify_s3_file_exists,
)
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
    StorageServiceException,
    ValidationException,
)
from shared.services.job_lifecycle_sync import get_sync_job_lifecycle_service
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
    SyncRedisServiceFactory,
)
from shared.utils.url_file_type import resolve_file_extension_sync


def upload_url_file(
    job_id: str,
    source_url: str,
    user_id: str | None,
    job_type: str | None = None,
) -> dict[str, Any]:
    del user_id, job_type

    lifecycle_service = get_sync_job_lifecycle_service()
    redis_service = SyncRedisServiceFactory.get_service()
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if not job_info:
        metadata_service = SyncJobMetadataService(redis_service)
        job_metadata = metadata_service.get_metadata(job_id)
        if job_metadata:
            s3_key = job_metadata.get("s3_key")
        else:
            raise NotFoundException(
                resource="JobInfo",
                resource_id=job_id,
                internal_message="Job info not found in Redis or Metadata",
            )
    else:
        s3_key = job_info.get("s3_key")

    if not s3_key:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="s3_key",
            internal_message=f"Missing s3_key in Redis job info for job_id={job_id}",
        )

    lifecycle_service.update_progress(
        job_id, progress=3, message="Validating URL file type..."
    )
    file_extension = resolve_file_extension_sync(source_url)
    if not file_extension:
        supported_formats = ", ".join(sorted(settings.get_supported_extensions()))
        raise ValidationException(
            user_message="Unsupported file type",
            violations=[
                {
                    "field": "file_extension",
                    "description": f"Must be one of: {supported_formats}",
                }
            ],
        )

    lifecycle_service.update_progress(
        job_id, progress=10, message="Downloading file from URL..."
    )
    try:
        temp_file_path = download_file_from_url(source_url)
    except Exception as exc:
        raise ValidationException(
            user_message="Failed to download file from URL",
            violations=[
                {
                    "field": "source_url",
                    "description": "Could not download file from the provided URL",
                }
            ],
            internal_message=(
                f"Failed to download file from URL: {source_url}, error: {exc}"
            ),
        )

    try:
        lifecycle_service.update_progress(
            job_id, progress=30, message="Validating file size..."
        )
        file_size = os.path.getsize(temp_file_path)
        if file_size > settings.MAX_FILE_SIZE:
            limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
            raise ValidationException(
                user_message=(
                    f"File size exceeds limit (max {limit_mb}MB for {file_extension})"
                ),
                violations=[
                    {
                        "field": "file_size",
                        "description": (
                            f"Size {file_size} bytes exceeds limit of "
                            f"{settings.MAX_FILE_SIZE} bytes"
                        ),
                    }
                ],
            )

        lifecycle_service.update_progress(
            job_id, progress=50, message="Uploading file to S3..."
        )
        upload_to_s3(temp_file_path, str(s3_key), settings.S3_BUCKET_NAME)
        logger.info(f"File uploaded to S3: {s3_key}")

    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            logger.debug(f"Temp file cleaned up: {temp_file_path}")

    lifecycle_service.update_progress(
        job_id, progress=80, message="Verifying upload result..."
    )
    file_info = verify_s3_file_exists(str(s3_key))
    if not file_info.get("exists"):
        raise StorageServiceException(
            user_message="We failed to verify your file upload",
            internal_message=f"S3 file verification failed for {s3_key}",
        )

    lifecycle_service.update_progress(
        job_id,
        progress=100,
        message="URL file upload complete, waiting for processing...",
    )
    logger.info(
        f"URL file upload complete, waiting for S3 webhook: {job_id} -> {s3_key}"
    )

    return {
        "status": "success",
        "job_id": job_id,
        "s3_key": s3_key,
        "file_size": file_info.get("size"),
    }
