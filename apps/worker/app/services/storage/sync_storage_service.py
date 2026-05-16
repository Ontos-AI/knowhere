"""Sync adapter for shared Job file storage used by worker tasks."""

import os
from typing import Any

from loguru import logger

from shared.core.config import settings
from shared.services.storage.job_file_storage import JobFileStorage


def get_storage_adapter() -> JobFileStorage:
    """Get the shared job file storage module for sync worker operations."""
    return JobFileStorage()


def verify_s3_file_exists(s3_key: str, bucket: str | None = None) -> dict[str, Any]:
    """Verify an uploaded source file exists."""
    storage = get_storage_adapter()
    return storage.verify_exists(
        s3_key,
        bucket=bucket or settings.S3_BUCKET_NAME,
    )


def generate_download_url(
    s3_key: str, bucket: str | None = None, expires_in: int = 3600
) -> dict[str, Any]:
    """Generate a presigned download URL for a stored object."""
    storage = get_storage_adapter()
    return storage.generate_download_url(
        s3_key,
        bucket=bucket or settings.S3_BUCKET_NAME,
        expires_in=expires_in,
    )


def upload_to_s3(local_file_path: str, s3_key: str, bucket: str) -> None:
    """Upload a local file using the shared job file storage rules."""
    storage = get_storage_adapter()
    storage.upload_local_file(local_file_path, s3_key, bucket=bucket)


def download_s3_object_to_temp(
    s3_key: str,
    suffix: str,
    temp_dir: str,
    bucket: str | None = None,
) -> str:
    """Download an object-storage file into a task-local temp file."""
    storage = get_storage_adapter()
    return storage.download_to_temp(
        s3_key,
        suffix=suffix,
        temp_dir=temp_dir,
        bucket=bucket or settings.S3_BUCKET_NAME,
    )


def upload_zip_result(job_id: str, zip_file_path: str) -> str:
    """Upload ZIP result file to S3 and cleanup temp file."""
    storage = get_storage_adapter()
    results_bucket = storage.results_bucket
    s3_key = storage.build_result_zip_key(job_id=job_id)
    upload_to_s3(zip_file_path, s3_key, results_bucket)
    logger.info(f"Result ZIP uploaded: job_id={job_id}, key={s3_key}")
    try:
        if os.path.exists(zip_file_path):
            os.remove(zip_file_path)
    except Exception as e:
        logger.warning(f"Failed to cleanup temp ZIP: {e}")
    return s3_key


def download_file_from_url(file_url: str) -> str:
    """Download a URL file through SSRF validation and IP pinning."""
    storage = get_storage_adapter()
    temp_dir = getattr(settings, "TMP_PATH", "/tmp")
    return storage.download_file_from_url(file_url, temp_dir=temp_dir)
