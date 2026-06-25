"""
OSS存储适配器实现（阿里云对象存储）
"""
import base64
import json
import os
from typing import Any, BinaryIO, Dict, Iterator, Optional

from loguru import logger

from shared.core.exceptions.domain_exceptions import StorageServiceException
from shared.services.storage.storage_adapter import StorageAdapter


# 延迟导入oss2，只在需要时导入
def _import_oss2():
    """延迟导入oss2模块"""
    try:
        import oss2
        from oss2.exceptions import NoSuchKey, NotFound, OssError
        return oss2, OssError, NoSuchKey, NotFound
    except ImportError as e:
        raise ImportError(
            "oss2 module not installed. When S3_TYPE=oss, please install oss2: pip install oss2>=2.18.0"
        ) from e


def _encode_callback_param(value: Dict[str, Any]) -> str:
    raw_value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw_value.encode("utf-8")).decode("ascii")


def _build_upload_callback_headers() -> Dict[str, str]:
    callback_url = os.getenv("API_WEBHOOK_ENDPOINT", "").strip()
    if not callback_url:
        return {}

    callback_body = {
        "events": [
            {
                "eventName": "ObjectCreated:PutObject",
                "eventSource": "acs:oss",
                "eventTime": "",
                "region": os.getenv("S3_REGION", ""),
                "oss": {
                    "bucket": {"name": "${bucket}"},
                    "object": {"key": "${object}"},
                },
            }
        ]
    }
    callback_config = {
        "callbackUrl": callback_url,
        "callbackBody": json.dumps(
            callback_body, ensure_ascii=False, separators=(",", ":")
        ),
        "callbackBodyType": "application/json",
    }
    return {"x-oss-callback": _encode_callback_param(callback_config)}


class OSSStorageAdapter(StorageAdapter):
    """OSS存储适配器（阿里云对象存储）"""
    
    def __init__(self, bucket, default_bucket_name: str):
        """
        初始化OSS适配器
        
        Args:
            bucket: OSS Bucket对象
            default_bucket_name: 默认存储桶名称
        """
        self.bucket = bucket
        self.default_bucket_name = default_bucket_name
    
    def _get_bucket_name(self, bucket: Optional[str] = None) -> str:
        """
        获取存储桶名称
        
        注意：OSS适配器使用单个Bucket对象，不支持跨bucket操作
        如果指定了不同的bucket，会记录警告但继续使用默认bucket
        """
        if bucket and bucket != self.default_bucket_name:
            logger.warning(f"OSS适配器不支持跨bucket操作，使用默认bucket: {self.default_bucket_name} 而非 {bucket}")
        return self.default_bucket_name
    
    def upload_file(self, local_path: str, key: str, bucket: Optional[str] = None) -> Dict[str, Any]:
        """上传本地文件到OSS"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            result = self.bucket.put_object_from_file(key, local_path)
            logger.debug(f"OSS上传成功: {key} -> {bucket_name}")
            return {
                "bucket": bucket_name,
                "key": key,
                "status": "success",
                "etag": result.etag
            }
        except OssError as e:
            logger.error(f"OSS upload failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS upload failed: {str(e)}",
                operation="upload_file",
                original_exception=e
            )
    
    def upload_fileobj(self, file_obj: BinaryIO, key: str, bucket: Optional[str] = None,
                      content_type: Optional[str] = None) -> Dict[str, Any]:
        """上传文件对象到OSS"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            headers = {}
            if content_type:
                headers['Content-Type'] = content_type
            
            data = file_obj.read()
            result = self.bucket.put_object(key, data, headers=headers if headers else None)
            logger.debug(f"OSS上传文件对象成功: {key} -> {bucket_name}")
            return {
                "bucket": bucket_name,
                "key": key,
                "status": "success",
                "etag": result.etag
            }
        except OssError as e:
            logger.error(f"OSS upload file object failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS upload file object failed: {str(e)}",
                operation="upload_fileobj",
                original_exception=e
            )
    
    def download_file(self, key: str, local_path: str, bucket: Optional[str] = None) -> str:
        """从OSS下载文件到本地"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            self.bucket.get_object_to_file(key, local_path)
            logger.debug(f"OSS下载成功: {bucket_name}/{key} -> {local_path}")
            return local_path
        except OssError as e:
            logger.error(f"OSS download failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS download failed: {str(e)}",
                operation="download_file",
                original_exception=e
            )
    
    def download_fileobj(self, key: str, bucket: Optional[str] = None) -> bytes:
        """从OSS下载文件对象"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            result = self.bucket.get_object(key)
            return result.read()
        except OssError as e:
            logger.error(f"OSS download file object failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS download file object failed: {str(e)}",
                operation="download_fileobj",
                original_exception=e
            )
    
    def delete_object(self, key: str, bucket: Optional[str] = None) -> bool:
        """删除OSS中的对象"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            self.bucket.delete_object(key)
            logger.debug(f"OSS删除成功: {bucket_name}/{key}")
            return True
        except OssError as e:
            logger.error(f"OSS删除失败: {e}")
            return False
    
    def list_objects(self, prefix: str = "", bucket: Optional[str] = None) -> Iterator[str]:
        """列出OSS中的对象"""
        oss2, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            for obj in oss2.ObjectIterator(self.bucket, prefix=prefix):
                yield obj.key
        except OssError as e:
            logger.error(f"OSS列出对象失败: {e}")
            return
    
    def generate_presigned_url(self, key: str, expiration: int = 3600,
                              bucket: Optional[str] = None, method: str = "GET",
                              headers: Optional[Dict[str, str]] = None) -> str:
        """生成OSS预签名URL"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            if method.upper() == "PUT":
                upload_headers = headers if headers is not None else {}
                upload_headers.update(_build_upload_callback_headers())
                url = self.bucket.sign_url(
                    'PUT',
                    key,
                    expiration,
                    headers=upload_headers or None,
                )
            else:
                url = self.bucket.sign_url('GET', key, expiration, headers=headers)
            
            logger.debug(f"OSS生成预签名URL成功: {key}")
            return url
        except OssError as e:
            logger.error(f"OSS generate presigned URL failed: {e}")
            raise StorageServiceException(
                internal_message=f"OSS generate presigned URL failed: {str(e)}",
                operation="generate_presigned_url",
                original_exception=e
            )
    
    def exists(self, key: str, bucket: Optional[str] = None) -> bool:
        """检查OSS中的对象是否存在"""
        _, OssError, _, _ = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
        try:
            return self.bucket.object_exists(key)
        except OssError as e:
            logger.error(f"OSS检查对象存在性失败: {e}")
            return False
    
    def get_object_size(self, key: str, bucket: Optional[str] = None) -> Optional[int]:
        """获取OSS对象大小"""
        _, OssError, NoSuchKey, NotFound = _import_oss2()
        bucket_name = self._get_bucket_name(bucket)
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
                original_exception=e
            )
