"""Async adapter for shared Job file storage."""

import asyncio
from typing import Any, Optional

from loguru import logger

from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.storage.job_file_storage import JobFileStorage


class FileUploadService:
    """Async adapter over the shared Job file storage module."""

    def __init__(self, *, storage: JobFileStorage | None = None) -> None:
        self._storage = storage or JobFileStorage()

    async def generate_upload_url(
        self, job_id: str, file_extension: str = ""
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._storage.generate_upload_url,
                job_id=job_id,
                file_extension=file_extension,
            )

        except Exception as e:
            logger.error(f"Failed to generate upload URL: {e}")
            raise StorageServiceException(
                internal_message=f"Failed to generate upload URL: {str(e)}",
                operation="generate_upload_url",
                original_exception=e,
            )

    async def generate_download_url(
        self, s3_key: str, bucket: Optional[str] = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._storage.generate_download_url,
                s3_key,
                bucket=bucket or self._storage.results_bucket,
                expires_in=expires_in,
            )

        except Exception as e:
            logger.error(f"Failed to generate download URL: {e}")
            raise StorageServiceException(
                internal_message=f"Failed to generate download URL: {str(e)}",
                operation="generate_download_url",
                original_exception=e,
            )

    async def verify_s3_file_exists(
        self, s3_key: str, bucket: Optional[str] = None
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._storage.verify_exists,
                s3_key,
                bucket=bucket or self._storage.uploads_bucket,
            )
        except Exception as e:
            logger.error(f"Failed to verify file existence: {e}")
            raise StorageServiceException(
                internal_message=f"Failed to verify file existence: {str(e)}",
                operation="verify_s3_file_exists",
                original_exception=e,
            )
