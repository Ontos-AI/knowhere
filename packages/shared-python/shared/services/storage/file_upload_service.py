"""
文件上传服务
"""

import asyncio
import json
import os
import uuid
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import StorageServiceException, KnowhereException


class FileUploadService:
    """文件上传服务（支持S3/OSS/MinIO）"""

    def __init__(self):
        self.adapter = settings.get_storage_adapter()
        self.uploads_bucket = settings.S3_BUCKET_NAME
        self.results_bucket = getattr(
            settings, "S3_RESULTS_BUCKET", settings.S3_BUCKET_NAME
        )

    async def handle_direct_upload(self, file_path: str, job_id: str) -> str:
        """
        处理直传文件

        Args:
            file_path: 本地文件路径
            job_id: 任务ID

        Returns:
            str: S3键
        """
        try:
            # 生成S3键
            file_extension = os.path.splitext(file_path)[1]
            s3_key = f"uploads/{job_id}{file_extension}"

            # 上传到S3
            await self._upload_to_s3(file_path, s3_key, self.uploads_bucket)

            logger.info(f"文件直传成功: {file_path} -> {s3_key}")
            return s3_key

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"文件直传失败: {e}")
            raise StorageServiceException(
                internal_message=f"文件直传失败: {str(e)}",
                operation="direct_upload",
                original_exception=e
            )

    async def handle_url_upload(self, file_url: str, job_id: str) -> str:
        """
        处理URL外链下载

        Args:
            file_url: 文件URL
            job_id: 任务ID

        Returns:
            str: S3键
        """
        try:
            # 下载文件到临时目录
            temp_file_path = await self._download_file_from_url(file_url)

            try:
                # 生成S3键
                file_extension = os.path.splitext(file_url.split("?")[0])[1]
                s3_key = f"uploads/{job_id}{file_extension}"

                # 上传到S3
                await self._upload_to_s3(temp_file_path, s3_key, self.uploads_bucket)

                logger.info(f"URL文件下载上传成功: {file_url} -> {s3_key}")
                return s3_key

            finally:
                # 清理临时文件
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"URL文件处理失败: {e}")
            raise StorageServiceException(
                internal_message=f"URL文件处理失败: {str(e)}",
                operation="url_upload",
                original_exception=e
            )

    async def generate_upload_url(
        self, job_id: str, file_extension: str = ""
    ) -> Dict[str, Any]:
        """
        生成预签名上传URL

        Args:
            job_id: 任务ID
            file_extension: 文件扩展名

        Returns:
            Dict: 包含上传URL和S3键的字典
        """
        try:
            s3_key = f"uploads/{job_id}{file_extension}"

            # 智能识别Content-Type
            content_type = self.get_content_type(file_extension)
            upload_headers = {"Content-Type": content_type}

            # 生成预签名URL（过期时间由JOB_WAITING_EXPIRE_SECONDS控制）
            upload_url = self.adapter.generate_presigned_url(
                s3_key,
                expiration=settings.JOB_WAITING_EXPIRE_SECONDS,
                bucket=self.uploads_bucket,
                method="PUT",
                headers=upload_headers
            )

            logger.info(f"Generated presigned upload URL for key: {s3_key}")

            return {
                "upload_url": upload_url,
                "s3_key": s3_key,
                "expires_in": settings.JOB_WAITING_EXPIRE_SECONDS,
                "upload_headers": upload_headers,
            }

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"生成上传URL失败: {e}")
            raise StorageServiceException(
                internal_message=f"生成上传URL失败: {str(e)}",
                operation="generate_upload_url",
                original_exception=e
            )

    async def generate_download_url(
        self, s3_key: str, bucket: Optional[str] = None, expires_in: int = 3600
    ) -> str:
        """
        生成预签名下载URL

        Args:
            s3_key: S3键
            bucket: 存储桶名称（可选）

        Returns:
            str: 下载URL
        """
        try:
            bucket_name = bucket or self.results_bucket

            # 生成预签名URL（1小时过期）
            download_url = self.adapter.generate_presigned_url(
                s3_key, expiration=expires_in, bucket=bucket_name, method="GET"
            )

            return {"download_url": download_url, "expires_in": expires_in}

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"生成下载URL失败: {e}")
            raise StorageServiceException(
                internal_message=f"生成下载URL失败: {str(e)}",
                operation="generate_download_url",
                original_exception=e
            )

    async def get_file_info(
        self, s3_key: str, bucket: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取文件信息

        Args:
            s3_key: S3键
            bucket: 存储桶名称（可选）

        Returns:
            Dict: 文件信息
        """
        try:
            bucket_name = bucket or self.results_bucket

            # 检查文件是否存在并获取大小
            if not self.adapter.exists(s3_key, bucket_name):
                return None
            
            size = self.adapter.get_object_size(s3_key, bucket_name)
            return {
                "size": size,
                "content_type": None,  # 适配器接口暂不支持获取content_type
                "last_modified": None,
                "etag": None,
            }

        except Exception as e:
            # 文件不存在
            if '404' in str(e) or 'not found' in str(e).lower():
                return None
            logger.error(f"获取文件信息失败: {e}")
            raise StorageServiceException(
                internal_message=f"获取文件信息失败: {str(e)}",
                operation="get_file_info",
                original_exception=e
            )

    async def upload_result_file(
        self, local_file_path: str, job_id: str, file_extension: str = ""
    ) -> str:
        """
        上传结果文件

        Args:
            local_file_path: 本地文件路径
            job_id: 任务ID
            file_extension: 文件扩展名

        Returns:
            str: S3键
        """
        try:
            s3_key = f"results/{job_id}{file_extension}"
            await self._upload_to_s3(local_file_path, s3_key, self.results_bucket)

            logger.info(f"结果文件上传成功: {local_file_path} -> {s3_key}")
            return s3_key

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"结果文件上传失败: {e}")
            raise StorageServiceException(
                internal_message=f"结果文件上传失败: {str(e)}",
                operation="upload_result_file",
                original_exception=e
            )

    async def upload_json_result(
        self,
        job_id: str,
        result_data: Dict[str, Any],
        *,
        content_type: str = "application/json",
    ) -> str:
        """上传JSON结果文件（已废弃，保留用于兼容）"""
        try:
            s3_key = f"results/{job_id}.json"
            from io import BytesIO
            body = json.dumps(result_data, ensure_ascii=False).encode("utf-8")
            self.adapter.upload_fileobj(
                BytesIO(body),
                s3_key,
                bucket=self.results_bucket,
                content_type=content_type
            )
            logger.info(f"结果JSON上传成功: job_id={job_id}, key={s3_key}")
            return s3_key
        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"上传结果JSON失败: {e}")
            raise StorageServiceException(
                internal_message=f"上传结果JSON失败: {str(e)}",
                operation="upload_json_result",
                original_exception=e
            )

    async def upload_zip_result(
        self,
        job_id: str,
        zip_file_path: str,
    ) -> str:
        """上传ZIP结果文件"""
        try:
            s3_key = f"results/{job_id}.zip"
            await self._upload_to_s3(zip_file_path, s3_key, self.results_bucket)
            logger.info(f"结果ZIP上传成功: job_id={job_id}, key={s3_key}")
            
            # 清理临时文件
            try:
                if os.path.exists(zip_file_path):
                    os.remove(zip_file_path)
            except Exception as e:
                logger.warning(f"清理临时ZIP文件失败: {e}")
            
            return s3_key
        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"上传结果ZIP失败: {e}")
            raise StorageServiceException(
                internal_message=f"上传结果ZIP失败: {str(e)}",
                operation="upload_zip_result",
                original_exception=e
            )

    def _ensure_bucket_exists(self, bucket_name: str) -> bool:
        """
        确保存储桶存在，如果不存在则创建

        Args:
            bucket_name: 存储桶名称

        Returns:
            bool: 是否成功
        """
        try:
            # 对于适配器模式，尝试访问bucket来验证其是否存在
            # 通过尝试列出对象来检查bucket是否存在
            adapter = settings.get_storage_adapter()
            list(adapter.list_objects(prefix="", bucket=bucket_name))
            logger.debug(f"存储桶 {bucket_name} 可访问")
            return True
        except Exception as e:
            # bucket不存在或无法访问
            # 注意：对于OSS，bucket需要预先创建，这里只检查可访问性
            logger.warning(f"存储桶 {bucket_name} 可能不存在或无法访问: {e}")
            # 对于生产环境，bucket应该预先创建，这里返回True继续执行
            # 如果需要严格检查，可以返回False
            return True

    async def _ensure_bucket_exists_async(self, bucket_name: str) -> bool:
        """
        异步确保存储桶存在，如果不存在则创建

        Args:
            bucket_name: 存储桶名称

        Returns:
            bool: 是否成功
        """
        def _check_and_create():
            try:
                # 对于适配器模式，尝试访问bucket来验证其是否存在
                adapter = settings.get_storage_adapter()
                list(adapter.list_objects(prefix="", bucket=bucket_name))
                logger.debug(f"存储桶 {bucket_name} 可访问")
                return True
            except Exception as e:
                # bucket不存在或无法访问
                logger.warning(f"存储桶 {bucket_name} 可能不存在或无法访问: {e}")
                # 对于生产环境，bucket应该预先创建，这里返回True继续执行
                return True

        # 在线程池中执行同步操作
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _check_and_create)

    async def _upload_to_s3(self, local_file_path: str, s3_key: str, bucket: str):
        """上传文件到S3"""
        # 确保存储桶存在
        if not await self._ensure_bucket_exists_async(bucket):
            raise StorageServiceException(
                internal_message=f"无法确保存储桶 {bucket} 存在",
                operation="ensure_bucket"
            )

        def _upload():
            self.adapter.upload_file(local_file_path, s3_key, bucket)

        # 在线程池中执行同步上传
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload)

    async def download_from_s3(self, s3_key: str, bucket: Optional[str] = None) -> str:
        """从S3下载文件到本地临时目录"""
        import uuid

        if bucket is None:
            bucket = settings.S3_BUCKET_NAME

        # 创建临时文件
        temp_dir = getattr(settings, "TMP_PATH", "/tmp")
        os.makedirs(temp_dir, exist_ok=True)

        # 生成临时文件名，保持原文件扩展名
        file_extension = os.path.splitext(s3_key)[1]
        temp_filename = f"temp_{uuid.uuid4().hex}{file_extension}"
        temp_file_path = os.path.join(temp_dir, temp_filename)

        try:
            # 使用适配器下载文件
            def _download():
                self.adapter.download_file(s3_key, temp_file_path, bucket)

            # 在事件循环中执行同步操作
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _download)

            return temp_file_path

        except KnowhereException:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise StorageServiceException(
                internal_message=f"从S3下载文件失败: {str(e)}",
                operation="download_from_s3",
                original_exception=e
            )

    async def _download_file_from_url(self, file_url: str) -> str:
        """从URL下载文件到临时目录"""
        temp_dir = getattr(settings, "TMP_PATH", "/tmp")
        os.makedirs(temp_dir, exist_ok=True)

        # 生成临时文件名
        temp_filename = f"temp_{uuid.uuid4().hex}"
        temp_file_path = os.path.join(temp_dir, temp_filename)

        try:
            # 配置aiohttp客户端，优化下载性能
            timeout = aiohttp.ClientTimeout(total=300, connect=30)  # 5分钟总超时，30秒连接超时
            connector = aiohttp.TCPConnector(
                limit=100,  # 总连接池大小
                limit_per_host=30,  # 每个主机的连接数
                ttl_dns_cache=300,  # DNS缓存5分钟
                use_dns_cache=True,
            )
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={'User-Agent': 'Knowhere-FileDownloader/1.0'}
            ) as session:
                async with session.get(file_url) as response:
                    if response.status != 200:
                        raise StorageServiceException(
                            internal_message=f"下载失败，状态码: {response.status}",
                            operation="download_from_url"
                        )

                    # 使用更大的chunk大小提高下载速度
                    with open(temp_file_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(65536):  # 64KB chunks
                            f.write(chunk)

            return temp_file_path

        except KnowhereException:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            raise StorageServiceException(
                internal_message=f"下载文件失败: {str(e)}",
                operation="download_from_url",
                original_exception=e
            )

    async def verify_s3_file_exists(
        self, s3_key: str, bucket: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        验证S3文件是否存在

        Args:
            s3_key: S3键
            bucket: 存储桶名称（可选）

        Returns:
            Dict: 文件信息或 {"exists": False}
        """
        try:
            bucket_name = bucket or self.uploads_bucket

            # 使用适配器检查文件是否存在
            exists = self.adapter.exists(s3_key, bucket_name)
            if not exists:
                return {"exists": False}
            
            size = self.adapter.get_object_size(s3_key, bucket_name)
            return {
                "exists": True,
                "size": size,
                "content_type": None,
                "last_modified": None,
                "etag": None,
            }

        except Exception as e:
            # 文件不存在或其他错误
            if '404' in str(e) or 'not found' in str(e).lower():
                return {"exists": False}
            logger.error(f"验证文件存在性失败: {e}")
            raise StorageServiceException(
                internal_message=f"验证文件存在性失败: {str(e)}",
                operation="verify_s3_file_exists",
                original_exception=e
            )

    def get_content_type(self, file_extension: str) -> str:
        """
        根据文件扩展名返回Content-Type

        Args:
            file_extension: 文件扩展名（如 .pdf, .docx）

        Returns:
            str: Content-Type
        """
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

    async def get_file_url(
        self, s3_key: str, bucket: Optional[str] = None, expires_in: int = 3600
    ) -> str:
        """
        通过S3键获取文件URL

        Args:
            s3_key: S3键
            bucket: 存储桶名称（可选）
            expires_in: URL过期时间（秒），默认1小时

        Returns:
            str: 文件URL
        """
        try:
            bucket_name = bucket or self.uploads_bucket

            # 生成预签名URL
            file_url = self.adapter.generate_presigned_url(
                s3_key, expiration=expires_in, bucket=bucket_name, method="GET"
            )

            logger.info(f"生成文件URL成功: {s3_key} -> {file_url}")
            return file_url

        except KnowhereException:
            raise
        except Exception as e:
            logger.error(f"获取文件URL失败: {e}")
            raise StorageServiceException(
                internal_message=f"获取文件URL失败: {str(e)}",
                operation="get_file_url",
                original_exception=e
            )

    def generate_s3_key(
        self, job_id: str, file_extension: str = "", prefix: str = "uploads"
    ) -> str:
        """
        生成S3键

        Args:
            job_id: 任务ID
            file_extension: 文件扩展名
            prefix: 前缀（uploads 或 results）

        Returns:
            str: S3键
        """
        return f"{prefix}/{job_id}{file_extension}"
