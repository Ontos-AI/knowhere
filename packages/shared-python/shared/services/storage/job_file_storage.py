from __future__ import annotations

import os
import tempfile
from typing import Any, BinaryIO

from shared.core.config import settings
from shared.core.config.storage import get_cached_storage_adapter
from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.storage.storage_adapter import StorageAdapter
from shared.services.http.pinned_outbound import download_pinned_outbound_file
from shared.services.http.url_security import validate_http_url_and_resolve_ip


class JobFileStorage:
    """Own storage rules for Job source files and Job Result bundles."""

    def __init__(
        self,
        *,
        storage_adapter: StorageAdapter | None = None,
        uploads_bucket: str | None = None,
        results_bucket: str | None = None,
    ) -> None:
        self._storage_adapter = storage_adapter
        self.uploads_bucket = uploads_bucket or settings.S3_BUCKET_NAME
        self.results_bucket = results_bucket or getattr(
            settings,
            "S3_RESULTS_BUCKET",
            settings.S3_BUCKET_NAME,
        )

    @property
    def storage_adapter(self) -> StorageAdapter:
        if self._storage_adapter is None:
            self._storage_adapter = get_cached_storage_adapter()
        return self._storage_adapter

    def build_upload_key(self, *, job_id: str, file_extension: str = "") -> str:
        return f"uploads/{job_id}{file_extension}"

    def build_result_key(self, *, job_id: str, file_extension: str = "") -> str:
        return f"results/{job_id}{file_extension}"

    def build_result_zip_key(self, *, job_id: str) -> str:
        return self.build_result_key(job_id=job_id, file_extension=".zip")

    def build_result_raw_prefix(self, *, job_id: str) -> str:
        return f"results/{job_id}/"

    def generate_upload_url(
        self,
        *,
        job_id: str,
        file_extension: str = "",
    ) -> dict[str, Any]:
        storage_key = self.build_upload_key(
            job_id=job_id,
            file_extension=file_extension,
        )
        content_type = self.get_content_type(file_extension)
        upload_url = self.storage_adapter.generate_presigned_url(
            storage_key,
            expiration=settings.JOB_WAITING_EXPIRE_SECONDS,
            bucket=self.uploads_bucket,
            method="PUT",
            headers={"Content-Type": content_type},
        )
        return {
            "upload_url": upload_url,
            "s3_key": storage_key,
            "expires_in": settings.JOB_WAITING_EXPIRE_SECONDS,
            "upload_headers": {"Content-Type": content_type},
        }

    def generate_download_url(
        self,
        storage_key: str,
        *,
        bucket: str,
        expires_in: int = 3600,
    ) -> dict[str, Any]:
        download_url = self.storage_adapter.generate_presigned_url(
            storage_key,
            expiration=expires_in,
            bucket=bucket,
            method="GET",
        )
        return {"download_url": download_url, "expires_in": expires_in}

    def generate_upload_download_url(
        self,
        storage_key: str,
        *,
        expires_in: int = 3600,
    ) -> dict[str, Any]:
        return self.generate_download_url(
            storage_key,
            bucket=self.uploads_bucket,
            expires_in=expires_in,
        )

    def verify_exists(
        self,
        storage_key: str,
        *,
        bucket: str,
    ) -> dict[str, Any]:
        try:
            if not self.storage_adapter.exists(storage_key, bucket):
                return {"exists": False}

            size = self.storage_adapter.get_object_size(storage_key, bucket)
            return {
                "exists": True,
                "size": size,
                "content_type": None,
                "last_modified": None,
                "etag": None,
            }
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return {"exists": False}
            raise StorageServiceException(
                internal_message=f"Storage file verification failed: {exc}",
                operation="verify_exists",
                original_exception=exc,
            ) from exc

    def verify_upload_exists(self, storage_key: str) -> dict[str, Any]:
        return self.verify_exists(storage_key, bucket=self.uploads_bucket)

    def upload_local_file(
        self,
        local_file_path: str,
        storage_key: str,
        *,
        bucket: str,
    ) -> dict[str, Any]:
        try:
            return self.storage_adapter.upload_file(local_file_path, storage_key, bucket)
        except Exception as exc:
            raise StorageServiceException(
                internal_message=f"Storage upload failed: {exc}",
                operation="upload_local_file",
                original_exception=exc,
            ) from exc

    def upload_source_file(
        self,
        local_file_path: str,
        storage_key: str,
    ) -> dict[str, Any]:
        return self.upload_local_file(
            local_file_path,
            storage_key,
            bucket=self.uploads_bucket,
        )

    def delete_object(
        self,
        storage_key: str,
        *,
        bucket: str,
    ) -> bool:
        try:
            return self.storage_adapter.delete_object(storage_key, bucket)
        except Exception as exc:
            raise StorageServiceException(
                internal_message=f"Storage delete failed: {exc}",
                operation="delete_object",
                original_exception=exc,
            ) from exc

    def delete_upload_file(self, storage_key: str) -> bool:
        return self.delete_object(storage_key, bucket=self.uploads_bucket)

    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        storage_key: str,
        *,
        bucket: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.storage_adapter.upload_fileobj(
                file_obj,
                storage_key,
                bucket=bucket,
                content_type=content_type,
            )
        except Exception as exc:
            raise StorageServiceException(
                internal_message=f"Storage upload file object failed: {exc}",
                operation="upload_fileobj",
                original_exception=exc,
            ) from exc

    def download_to_path(
        self,
        storage_key: str,
        local_path: str,
        *,
        bucket: str,
    ) -> str:
        try:
            return self.storage_adapter.download_file(storage_key, local_path, bucket)
        except Exception as exc:
            raise StorageServiceException(
                internal_message=f"Storage download failed: {exc}",
                operation="download_to_path",
                original_exception=exc,
            ) from exc

    def download_to_temp(
        self,
        storage_key: str,
        *,
        suffix: str,
        temp_dir: str,
        bucket: str,
    ) -> str:
        local_temp_path: str | None = None

        try:
            os.makedirs(temp_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=suffix,
                dir=temp_dir,
            ) as temp_file:
                local_temp_path = temp_file.name

            self.download_to_path(
                storage_key,
                local_temp_path,
                bucket=bucket,
            )
            return local_temp_path
        except Exception as exc:
            if local_temp_path and os.path.exists(local_temp_path):
                os.remove(local_temp_path)
            raise StorageServiceException(
                internal_message=(
                    "Failed to download object-storage file to temp path: "
                    f"storage_key={storage_key}, temp_dir={temp_dir}, error={exc}"
                ),
                operation="download_to_temp",
                original_exception=exc,
            ) from exc

    def download_upload_to_temp(
        self,
        storage_key: str,
        *,
        suffix: str,
        temp_dir: str,
    ) -> str:
        return self.download_to_temp(
            storage_key,
            suffix=suffix,
            temp_dir=temp_dir,
            bucket=self.uploads_bucket,
        )

    def download_file_from_url(
        self,
        file_url: str,
        *,
        temp_dir: str | None = None,
    ) -> str:
        temp_file_path = ""
        try:
            validation = validate_http_url_and_resolve_ip(file_url)
            if not validation.is_valid or not validation.validated_ip:
                raise StorageServiceException(
                    internal_message=f"Invalid URL: {validation.error_message}",
                    operation="download_from_url",
                )

            effective_temp_dir = temp_dir or getattr(settings, "TMP_PATH", "/tmp")
            os.makedirs(effective_temp_dir, exist_ok=True)
            download_result = download_pinned_outbound_file(
                url=validation.url,
                pinned_ip=validation.validated_ip,
                timeout_seconds=300,
                user_agent="Knowhere-FileDownloader/1.0",
                temp_dir=effective_temp_dir,
            )
            temp_file_path = download_result.temp_file_path
            return temp_file_path
        except StorageServiceException:
            raise
        except Exception as exc:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise StorageServiceException(
                internal_message=f"Failed to download file: {exc}",
                operation="download_from_url",
                original_exception=exc,
            ) from exc

    @staticmethod
    def get_content_type(file_extension: str) -> str:
        content_types = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".csv": "text/csv",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".json": "application/json",
            ".xml": "application/xml",
            ".html": "text/html",
            ".htm": "text/html",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".svg": "image/svg+xml",
            ".zip": "application/zip",
            ".rar": "application/x-rar-compressed",
            ".7z": "application/x-7z-compressed",
            ".tar": "application/x-tar",
            ".gz": "application/gzip",
        }
        return content_types.get(file_extension.lower(), "application/octet-stream")
