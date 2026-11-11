"""Async adapter for shared Job file storage."""

import asyncio
from typing import Any, Literal, Optional

from loguru import logger

from shared.core.exceptions.domain_exceptions import (
    StorageServiceException,
    UnavailableException,
)
from shared.models.schemas.source_integrity import SourceIntegrity
from shared.services.storage.async_presigner import AsyncStoragePresigner
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

    async def generate_verified_upload_url(
        self,
        *,
        storage_key: str,
        file_extension: str,
        source_integrity: SourceIntegrity,
        expiration: int,
    ) -> dict[str, Any]:
        """Issue a SigV4 PUT capability bound to exact bytes and a new key."""
        headers: dict[str, str] = {
            "Content-Type": self._storage.get_content_type(file_extension),
            "Content-Length": str(source_integrity.size_bytes),
            "If-None-Match": "*",
            "x-amz-checksum-sha256": source_integrity.checksum_base64(),
        }
        try:
            signed = await AsyncStoragePresigner().generateUrlWithExpiry(
                storage_key,
                bucket=self._storage.uploads_bucket,
                method="PUT",
                headers=headers,
                expiration=expiration,
                source_integrity=source_integrity,
            )
        except TimeoutError as error:
            raise UnavailableException(
                internal_message="Verified source upload signing timed out",
                retry_after=1,
                user_message="Could not issue an upload URL. Retry shortly.",
            ) from error
        return {
            "upload_url": signed.url,
            "s3_key": storage_key,
            "expires_in": signed.expiresIn,
            "upload_headers": headers,
        }

    async def verify_upload_integrity(
        self, storage_key: str, source_integrity: SourceIntegrity
    ) -> Literal["matched", "missing", "mismatch"]:
        try:
            return await asyncio.to_thread(
                self._storage.verify_upload_integrity, storage_key, source_integrity
            )
        except Exception as error:
            raise UnavailableException(
                internal_message="Could not read the stored source checksum from S3",
                retry_after=1,
                user_message="Could not verify the uploaded file. Retry shortly.",
            ) from error

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
