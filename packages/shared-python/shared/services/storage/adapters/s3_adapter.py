"""S3 storage adapter implementation for AWS S3 and MinIO."""

from typing import Any, BinaryIO, Dict, Iterator, Optional

from botocore.client import BaseClient
from botocore.exceptions import ClientError
from loguru import logger

from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.storage.storage_adapter import StorageAdapter


class S3StorageAdapter(StorageAdapter):
    """S3 storage adapter supporting AWS S3 and MinIO."""

    def __init__(self, s3_client: BaseClient, default_bucket: str):
        """
        Initialize the S3 adapter.

        Args:
            s3_client: boto3 S3 client.
            default_bucket: Default bucket name.
        """
        self.s3_client = s3_client
        self.default_bucket = default_bucket

    def _get_bucket(self, bucket: Optional[str] = None) -> str:
        """Get the effective bucket name."""
        return bucket or self.default_bucket

    def upload_file(
        self, local_path: str, key: str, bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload a local file to S3."""
        bucket_name = self._get_bucket(bucket)
        try:
            self.s3_client.upload_file(local_path, bucket_name, key)
            logger.debug(f"S3 upload succeeded: {key} -> {bucket_name}")
            return {"bucket": bucket_name, "key": key, "status": "success"}
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 upload_file failed: bucket={bucket_name}, key={key}, error_code={error_code}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 upload failed: {str(e)}",
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
        """Upload a file object to S3."""
        bucket_name = self._get_bucket(bucket)
        try:
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type

            self.s3_client.upload_fileobj(
                file_obj, bucket_name, key, ExtraArgs=extra_args if extra_args else None
            )
            logger.debug(f"S3 object upload succeeded: {key} -> {bucket_name}")
            return {"bucket": bucket_name, "key": key, "status": "success"}
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 upload_fileobj failed: bucket={bucket_name}, key={key}, error_code={error_code}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 upload file object failed: {str(e)}",
                operation="upload_fileobj",
                original_exception=e,
            )

    def download_file(
        self, key: str, local_path: str, bucket: Optional[str] = None
    ) -> str:
        """Download an S3 object to a local file."""
        bucket_name = self._get_bucket(bucket)
        try:
            self.s3_client.download_file(bucket_name, key, local_path)
            logger.debug(f"S3 download succeeded: {bucket_name}/{key} -> {local_path}")
            return local_path
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 download_file failed: bucket={bucket_name}, key={key}, error_code={error_code}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 download failed: {str(e)}",
                operation="download_file",
                original_exception=e,
            )

    def download_fileobj(self, key: str, bucket: Optional[str] = None) -> bytes:
        """Download an S3 object into memory."""
        bucket_name = self._get_bucket(bucket)
        try:
            response = self.s3_client.get_object(Bucket=bucket_name, Key=key)
            return response["Body"].read()
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 download_fileobj failed: bucket={bucket_name}, key={key}, error_code={error_code}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 download file object failed: {str(e)}",
                operation="download_fileobj",
                original_exception=e,
            )

    def delete_object(self, key: str, bucket: Optional[str] = None) -> bool:
        """Delete an S3 object."""
        bucket_name = self._get_bucket(bucket)
        try:
            self.s3_client.delete_object(Bucket=bucket_name, Key=key)
            logger.debug(f"S3 delete succeeded: {bucket_name}/{key}")
            return True
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 delete_object failed: bucket={bucket_name}, key={key}, error_code={error_code}, error={e}"
            )
            return False

    def list_objects(
        self, prefix: str = "", bucket: Optional[str] = None
    ) -> Iterator[str]:
        """List S3 objects."""
        bucket_name = self._get_bucket(bucket)
        try:
            paginator = self.s3_client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

            for page in pages:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        yield obj["Key"]
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 list_objects failed: bucket={bucket_name}, prefix={prefix}, error_code={error_code}, error={e}"
            )
            return

    def generate_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
        bucket: Optional[str] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """Generate an S3 presigned URL."""
        bucket_name = self._get_bucket(bucket)
        try:
            params = {"Bucket": bucket_name, "Key": key}

            # Include Content-Type in PUT signatures so object metadata matches
            # the request headers used by the client.
            if method.upper() == "PUT" and headers:
                content_type = headers.get("Content-Type") or headers.get(
                    "content-type"
                )
                if content_type:
                    params["ContentType"] = content_type

            if method.upper() == "GET" and headers:
                if contentType := headers.get("Content-Type"):
                    params["ResponseContentType"] = contentType
                if disposition := headers.get("Content-Disposition"):
                    params["ResponseContentDisposition"] = disposition
            if method.upper() == "PUT":
                url = self.s3_client.generate_presigned_url(
                    "put_object", Params=params, ExpiresIn=expiration
                )
            else:
                url = self.s3_client.generate_presigned_url(
                    "get_object", Params=params, ExpiresIn=expiration
                )

            logger.debug(f"S3 presigned URL generated successfully: {key}")
            return url
        except ClientError as e:
            error_code = (
                e.response["Error"].get("Code", "Unknown")
                if hasattr(e, "response")
                else "Unknown"
            )
            logger.error(
                f"S3 generate_presigned_url failed: bucket={bucket_name}, key={key}, method={method}, error_code={error_code}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 generate presigned URL failed: {str(e)}",
                operation="generate_presigned_url",
                original_exception=e,
            )

    def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """Check whether an S3 object exists."""
        bucket_name = self._get_bucket(bucket)
        try:
            self.s3_client.head_object(Bucket=bucket_name, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise StorageServiceException(
                internal_message=f"S3 check object exists failed: {str(e)}",
                operation="exists",
                original_exception=e,
            )

    def get_object_size(self, key: str, bucket: Optional[str] = None) -> Optional[int]:
        """Get an S3 object size."""
        bucket_name = self._get_bucket(bucket)
        try:
            response = self.s3_client.head_object(Bucket=bucket_name, Key=key)
            return response.get("ContentLength")
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return None
            logger.error(
                f"S3 get_object_size failed: bucket={bucket_name}, key={key}, error={e}"
            )
            raise StorageServiceException(
                internal_message=f"S3 get object size failed: {str(e)}",
                operation="get_object_size",
                original_exception=e,
            )
