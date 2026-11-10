"""OSS storage adapter implementation for Alibaba Cloud Object Storage."""

from typing import Any, BinaryIO, Dict, Iterator, Optional

from loguru import logger

from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.storage.storage_adapter import StorageAdapter
from shared.services.storage.object_inventory_page import ObjectInventoryPage


# Lazy-import oss2 so the dependency is only required when OSS is enabled.
def _import_oss2():
    """Import oss2 lazily."""
    try:
        import oss2
        from oss2.exceptions import NoSuchKey, NotFound, OssError

        return oss2, OssError, NoSuchKey, NotFound
    except ImportError as e:
        raise ImportError(
            "oss2 module not installed. When S3_TYPE=oss, please install oss2: pip install oss2>=2.18.0"
        ) from e


class OSSStorageAdapter(StorageAdapter):
    """OSS storage adapter for Alibaba Cloud Object Storage."""

    def __init__(self, bucket, default_bucket_name: str):
        """
        Initialize the OSS adapter.

        Args:
            bucket: OSS Bucket instance.
            default_bucket_name: Default bucket name.
        """
        self.bucket = bucket
        self.default_bucket_name = default_bucket_name

    def close(self) -> None:
        # oss2's session wrapper owns a requests.Session and has no close API.
        self.bucket.session.session.close()

    def _get_bucket_name(self, bucket: Optional[str] = None) -> str:
        """
        Get the effective bucket name.

        Note: the OSS adapter uses a single Bucket object and does not support
        cross-bucket operations. If a different bucket is requested, it logs a
        warning and continues with the default bucket.
        """
        if bucket and bucket != self.default_bucket_name:
            logger.warning(
                f"OSS adapter does not support cross-bucket operations; using the default bucket {self.default_bucket_name} instead of {bucket}"
            )
        return self.default_bucket_name

    def upload_file(
        self, local_path: str, key: str, bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload a local file to OSS."""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            result = self.bucket.put_object_from_file(key, local_path)
            logger.debug(f"OSS upload succeeded: {key} -> {bucket_name}")
            return {
                "bucket": bucket_name,
                "key": key,
                "status": "success",
                "etag": result.etag,
            }
        except OssError as e:
            logger.error(f"OSS upload failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS upload failed: {str(e)}",
                operation="upload_file",
                original_exception=e,
            )

    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        key: str,
        bucket: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a file object to OSS."""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            headers = {}
            if content_type:
                headers["Content-Type"] = content_type

            data = file_obj.read()
            result = self.bucket.put_object(
                key, data, headers=headers if headers else None
            )
            logger.debug(f"OSS object upload succeeded: {key} -> {bucket_name}")
            return {
                "bucket": bucket_name,
                "key": key,
                "status": "success",
                "etag": result.etag,
            }
        except OssError as e:
            logger.error(f"OSS upload file object failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS upload file object failed: {str(e)}",
                operation="upload_fileobj",
                original_exception=e,
            )

    def download_file(
        self, key: str, local_path: str, bucket: Optional[str] = None
    ) -> str:
        """Download an OSS object to a local file."""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            self.bucket.get_object_to_file(key, local_path)
            logger.debug(f"OSS download succeeded: {bucket_name}/{key} -> {local_path}")
            return local_path
        except OssError as e:
            logger.error(f"OSS download failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS download failed: {str(e)}",
                operation="download_file",
                original_exception=e,
            )

    def download_fileobj(self, key: str, bucket: Optional[str] = None) -> bytes:
        """Download an OSS object into memory."""
        _, OssError, _, _ = _import_oss2()
        self._get_bucket_name(bucket)
        try:
            result = self.bucket.get_object(key)
            return result.read()
        except OssError as e:
            logger.error(f"OSS download file object failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS download file object failed: {str(e)}",
                operation="download_fileobj",
                original_exception=e,
            )

    def delete_object(self, key: str, bucket: Optional[str] = None) -> bool:
        """Delete an OSS object."""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            self.bucket.delete_object(key)
            logger.debug(f"OSS delete succeeded: {bucket_name}/{key}")
            return True
        except OssError as e:
            logger.error(f"OSS delete failed: {e}")
            return False

    def list_objects(
        self, prefix: str = "", bucket: Optional[str] = None
    ) -> Iterator[str]:
        """List OSS objects."""
        oss2, OssError, _, _ = _import_oss2()
        self._get_bucket_name(bucket)
        try:
            for obj in oss2.ObjectIterator(self.bucket, prefix=prefix):
                yield obj.key
        except OssError as e:
            logger.error(f"OSS list objects failed: {e}")
            return

    def list_object_page(self, *, prefix: str, after: str | None = None,
                         limit: int = 100, bucket: str | None = None) -> ObjectInventoryPage:
        if not 1 <= limit <= 1000:
            raise ValueError("Object inventory page limit must be between 1 and 1000")
        self._get_bucket_name(bucket)
        response = self.bucket.list_objects(prefix=prefix, marker=after or "", max_keys=limit)
        objects: tuple[tuple[str, int], ...] = tuple((item.key, int(item.size)) for item in response.object_list)
        cursor: str | None = str(response.next_marker) if response.is_truncated else None
        if response.is_truncated and (not objects or not cursor or cursor == after):
            raise StorageServiceException(internal_message="Object inventory page did not advance", operation="list_objects")
        return ObjectInventoryPage(objects=objects, nextCursor=cursor)

    def generate_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
        bucket: Optional[str] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """Generate an OSS presigned URL."""
        _, OssError, _, _ = _import_oss2()
        self._get_bucket_name(bucket)
        try:
            if method.upper() == "PUT":
                url = self.bucket.sign_url("PUT", key, expiration, headers=headers)
            else:
                url = self.bucket.sign_url("GET", key, expiration, headers=headers)

            logger.debug(f"OSS presigned URL generated successfully: {key}")
            return url
        except OssError as e:
            logger.error(f"OSS generate presigned URL failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS generate presigned URL failed: {str(e)}",
                operation="generate_presigned_url",
                original_exception=e,
            )

    def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """Check whether an OSS object exists."""
        _, OssError, _, _ = _import_oss2()
        self._get_bucket_name(bucket)
        try:
            return self.bucket.object_exists(key)
        except OssError as e:
            logger.error(f"OSS object existence check failed: {e}")
            return False

    def get_object_size(self, key: str, bucket: Optional[str] = None) -> Optional[int]:
        """Get an OSS object size."""
        _, OssError, NoSuchKey, NotFound = _import_oss2()
        self._get_bucket_name(bucket)
        try:
            meta = self.bucket.head_object(key)
            return meta.content_length
        except (NoSuchKey, NotFound):
            return None
        except OssError as e:
            logger.error(f"OSS get object size failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS get object size failed: {str(e)}",
                operation="get_object_size",
                original_exception=e,
            )
