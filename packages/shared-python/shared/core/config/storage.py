"""Storage configuration."""

import os
import threading

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from pydantic import BaseModel, Field

from shared.core.exceptions.domain_exceptions import (
    DependencyMissingException,
    SystemSettingMissingException,
)

# Storage adapters are imported lazily to avoid circular imports.
# from shared.services.storage.adapters import S3StorageAdapter
# OSSStorageAdapter is imported only when S3_TYPE=oss.


class StorageConfig(BaseModel):
    model_config = {"extra": "ignore"}  # Ignore unrelated fields.
    """Storage configuration."""

    # Storage backend selection.
    S3_TYPE: str = Field(
        default="s3",
        description="Storage backend: s3, oss, minio, or filesystem",
    )

    # Shared S3-style configuration used by S3, OSS, and MinIO.
    S3_BUCKET_NAME: str = Field(..., description="Bucket name")
    S3_ACCESS_KEY_ID: str = Field(..., description="Access key ID")
    S3_SECRET_ACCESS_KEY: str = Field(..., description="Secret access key")
    S3_ENDPOINT_URL: str = Field(
        default="", description="Endpoint URL for S3-compatible services such as MinIO"
    )
    S3_PRIVATE_DOMAIN: str = Field(default="", description="Private asset domain")
    S3_TEMP_PATH: str = Field(..., description="Temporary path")
    OBJECT_STORAGE_LOCAL_ROOT: str = Field(
        default="",
        description="Local root directory used when S3_TYPE=filesystem",
    )

    # Advanced S3 client configuration.
    S3_REGION: str = Field(
        default="", description="S3 region; can stay empty for MinIO"
    )
    S3_USE_SSL: bool = Field(
        default=True, description="Use SSL/TLS for storage connections"
    )
    S3_ADDRESSING_STYLE: str = Field(
        default="auto", description="S3 addressing style: auto, path, or virtual"
    )

    # OSS-only configuration.
    OSS_ENDPOINT: str = Field(
        default="", description="OSS endpoint, for example oss-cn-hangzhou.aliyuncs.com"
    )

    # File-handling limits.
    MAX_FILE_SIZE: int = Field(
        default=314572800, description="Maximum file size in bytes"
    )
    MAX_PDF_PAGE_LIMIT: int = Field(
        default=200,
        ge=1,
        description="Maximum PDF page count for single-pass MinerU parsing. "
        "Documents exceeding this trigger the shard pipeline when enabled.",
    )
    OVERSIZED_PDF_SHARD_ENABLED: bool = Field(
        default=True,
        description="Enable doc_agent shard pipeline for PDFs exceeding MAX_PDF_PAGE_LIMIT. "
        "When False, oversized PDFs are rejected.",
    )
    OVERSIZED_PDF_SOFT_LIMIT: int = Field(
        default=1500,
        ge=1,
        description="Soft page limit for oversized PDF shard pipeline. "
        "Documents exceeding this are rejected with a contact-support message.",
    )
    MINERU_SHARD_CONCURRENCY: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum concurrent MinerU API calls for shard parsing.",
    )
    SUPPORTED_EXTENSIONS: str = Field(
        default=".doc,.docx,.pdf,.txt,.xls,.xlsx,.pptx,.jpg,.jpeg,.png,.md",
        description="Supported file extensions",
    )

    # S3 event-notification configuration.
    S3_WEBHOOK_AUTH_TOKEN: str = Field(
        default="", description="MinIO webhook authentication token"
    )

    # OSS event-notification configuration.
    OSS_EVENT_CALLBACK_KEY: str = Field(
        default="", description="OSS callback signing key"
    )
    OSS_EVENT_VERIFY_SIGNATURE: bool = Field(
        default=True, description="Verify OSS event signatures"
    )

    def get_s3_client(self) -> BaseClient:
        """Return an S3 client for S3-compatible backends."""
        # Build the client config.
        config_kwargs: dict[str, object] = {}

        # Configure addressing style.
        if self.S3_ADDRESSING_STYLE in ["path", "virtual"]:
            config_kwargs["s3"] = {"addressing_style": self.S3_ADDRESSING_STYLE}

        # Configure retries.
        config_kwargs["retries"] = {"max_attempts": 5, "mode": "standard"}

        config = Config(**config_kwargs) if config_kwargs else None

        # Build client kwargs.
        client_kwargs: dict[str, object] = {
            "service_name": "s3",
            "aws_access_key_id": self.S3_ACCESS_KEY_ID,
            "aws_secret_access_key": self.S3_SECRET_ACCESS_KEY,
        }

        # Add endpoint_url for MinIO or custom S3-compatible services.
        if self.S3_ENDPOINT_URL:
            client_kwargs["endpoint_url"] = self.S3_ENDPOINT_URL

        # Only pass region_name when it is configured.
        if self.S3_REGION:
            client_kwargs["region_name"] = self.S3_REGION

        # Configure SSL/TLS.
        if not self.S3_USE_SSL:
            client_kwargs["use_ssl"] = False

        # Only add config when extra settings are present.
        if config:
            client_kwargs["config"] = config

        return boto3.client(**client_kwargs)

    def get_oss_bucket(self):
        """Return an OSS Bucket object."""
        # Import oss2 lazily so non-OSS environments do not require it.
        try:
            import oss2
        except ImportError as e:
            raise DependencyMissingException(
                internal_message="oss2 module is not installed. When S3_TYPE=oss, please install: pip install oss2>=2.18.0",
                original_exception=e,
            ) from e

        if not self.OSS_ENDPOINT:
            raise SystemSettingMissingException(
                internal_message="OSS_ENDPOINT is required when S3_TYPE=oss"
            )

        auth = oss2.Auth(self.S3_ACCESS_KEY_ID, self.S3_SECRET_ACCESS_KEY)
        bucket = oss2.Bucket(auth, self.OSS_ENDPOINT, self.S3_BUCKET_NAME)
        return bucket

    def get_storage_adapter(self):
        """
        Return the storage adapter for the configured backend.

        This factory chooses the adapter from the S3_TYPE environment variable
        or the explicit config value.
        """
        storage_type = os.getenv("S3_TYPE", self.S3_TYPE).lower()

        if storage_type == "filesystem":
            from shared.services.storage.adapters import FileSystemStorageAdapter

            local_root = self.OBJECT_STORAGE_LOCAL_ROOT or os.path.join(
                self.S3_TEMP_PATH,
                "object-storage",
            )
            return FileSystemStorageAdapter(local_root, self.S3_BUCKET_NAME)

        if storage_type == "oss":
            # OSS storage adapter (imported lazily).
            from shared.services.storage.adapters.oss_adapter import OSSStorageAdapter

            bucket = self.get_oss_bucket()
            return OSSStorageAdapter(bucket, self.S3_BUCKET_NAME)
        else:
            # S3 storage adapter for AWS S3 and MinIO (imported lazily).
            from shared.services.storage.adapters import S3StorageAdapter

            s3_client = self.get_s3_client()
            return S3StorageAdapter(s3_client, self.S3_BUCKET_NAME)

    def get_supported_extensions(self) -> list:
        """Return the supported file extensions as a list."""
        return [ext.strip() for ext in self.SUPPORTED_EXTENSIONS.split(",")]


_cached_adapter = None
_cached_adapter_lock = threading.Lock()


def get_cached_storage_adapter():
    """
    Return a cached storage adapter singleton.
    Thread-safe with double-checked locking.
    """
    global _cached_adapter
    if _cached_adapter is None:
        with _cached_adapter_lock:
            if _cached_adapter is None:
                from shared.core.config import app_config

                _cached_adapter = app_config.get_storage_adapter()
    return _cached_adapter
