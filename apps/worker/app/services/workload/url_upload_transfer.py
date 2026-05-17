from __future__ import annotations

import os

from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    StorageServiceException,
    ValidationException,
)
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.url_file_type import resolve_file_extension_sync


def resolve_supported_url_extension(source_url: str) -> str:
    file_extension = resolve_file_extension_sync(source_url)
    if file_extension:
        return file_extension

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


def download_source_url_to_temp(source_url: str) -> str:
    storage = JobFileStorage()
    try:
        return storage.download_file_from_url(
            source_url,
            temp_dir=getattr(settings, "TMP_PATH", "/tmp"),
        )
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


def assert_temp_file_within_size_limit(
    *,
    temp_file_path: str,
    file_extension: str,
) -> int:
    file_size = os.path.getsize(temp_file_path)
    if file_size <= settings.MAX_FILE_SIZE:
        return file_size

    limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
    raise ValidationException(
        user_message=f"File size exceeds limit (max {limit_mb}MB for {file_extension})",
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


def upload_temp_file_to_source_storage(
    *,
    temp_file_path: str,
    s3_key: str,
) -> None:
    storage = JobFileStorage()
    storage.upload_source_file(temp_file_path, s3_key)
    logger.info(f"File uploaded to S3: {s3_key}")


def verify_source_upload(s3_key: str) -> dict[str, object]:
    storage = JobFileStorage()
    file_info = storage.verify_upload_exists(s3_key)
    if file_info.get("exists"):
        return dict(file_info)

    raise StorageServiceException(
        user_message="We failed to verify your file upload",
        internal_message=f"S3 file verification failed for {s3_key}",
    )


def cleanup_temp_file(temp_file_path: str | None) -> None:
    if not temp_file_path:
        return
    if os.path.exists(temp_file_path):
        os.remove(temp_file_path)
        logger.debug(f"Temp file cleaned up: {temp_file_path}")
