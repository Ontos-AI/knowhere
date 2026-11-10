"""
Unified storage adapter interface.
Supports consistent access patterns across S3, OSS, and MinIO backends.
"""

from abc import ABC, abstractmethod
from typing import Any, BinaryIO, Dict, Iterator, Optional
from shared.services.storage.object_inventory_page import ObjectInventoryPage


class StorageAdapter(ABC):
    """Abstract base class for storage adapters."""

    def close(self) -> None:
        """Release transport resources when this adapter is exclusively owned."""

    @abstractmethod
    def upload_file(
        self, local_path: str, key: str, bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Upload a local file to storage.

        Args:
            local_path: Local file path.
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            Upload result metadata.
        """

    @abstractmethod
    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        key: str,
        bucket: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file object to storage.

        Args:
            file_obj: Readable binary file object.
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.
            content_type: Content type.

        Returns:
            Upload result metadata.
        """

    @abstractmethod
    def download_file(
        self, key: str, local_path: str, bucket: Optional[str] = None
    ) -> str:
        """
        Download an object to a local file.

        Args:
            key: Object key in storage.
            local_path: Local file path.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            Local file path.
        """

    @abstractmethod
    def download_fileobj(self, key: str, bucket: Optional[str] = None) -> bytes:
        """
        Download an object into memory.

        Args:
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            File contents as bytes.
        """

    @abstractmethod
    def delete_object(self, key: str, bucket: Optional[str] = None) -> bool:
        """
        Delete an object from storage.

        Args:
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            Whether deletion succeeded.
        """

    @abstractmethod
    def list_objects(
        self, prefix: str = "", bucket: Optional[str] = None
    ) -> Iterator[str]:
        """
        List objects in storage.

        Args:
            prefix: Object-key prefix.
            bucket: Bucket name, or None to use the default bucket.

        Yields:
            Object keys.
        """

    @abstractmethod
    def list_object_page(self, *, prefix: str, after: str | None = None,
                         limit: int = 100, bucket: str | None = None) -> ObjectInventoryPage:
        """List one bounded page after an object key, including deleted keys.

        Errors must propagate: an unavailable inventory must not look like an
        empty successful scan or advance a durable collector cursor.
        """

    @abstractmethod
    def generate_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
        bucket: Optional[str] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Generate a presigned URL.

        Args:
            key: Object key in storage.
            expiration: Expiration time in seconds.
            bucket: Bucket name, or None to use the default bucket.
            method: HTTP method, such as GET or PUT.
            headers: Request headers included in the signature, such as Content-Type.

        Returns:
            Presigned URL.
        """

    @abstractmethod
    def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """
        Check whether an object exists.

        Args:
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            Whether the object exists.
        """

    @abstractmethod
    def get_object_size(self, key: str, bucket: Optional[str] = None) -> Optional[int]:
        """
        Get object size.

        Args:
            key: Object key in storage.
            bucket: Bucket name, or None to use the default bucket.

        Returns:
            Object size in bytes, or None when the object is missing.
        """
