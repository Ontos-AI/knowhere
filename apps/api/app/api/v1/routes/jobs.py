"""
统一Jobs API路由（符合PRD规范）
"""

from __future__ import annotations

from shared.core.billing import MicroDollar
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, cast
from urllib.parse import urlparse

from shared.core.config import settings
from shared.utils.url_file_type import resolve_file_extension_async
from shared.utils.error_details import normalize_error_details
from shared.core.database import get_db
from app.services.rate_limit.dependencies import (
    with_current_user,
    require_billing_limits,
    enforce_job_creation_capacity,
    CurrentUser,
)
from shared.core.state_machine.states import JobStatus
from shared.models.schemas.job import (ConfirmUploadRequest, JobCreate, JobList,
                                    JobResponse, JobResultResponse, StandardErrorObject)
from app.repositories.job_repository import JobRepository
from app.services.knowledge.kb_orchestrator import KBOrchestrator
from app.services.state_machine import JobStateMachine
from shared.services.storage.file_upload_service import FileUploadService
from fastapi import APIRouter, Depends, Query, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from shared.core.exceptions.domain_exceptions import (
    ValidationException,
    NotFoundException,
    PermissionDeniedException,
    JobOperationException,
    RateLimitException,
    UnavailableException,
)
from shared.core.exceptions.webhook_exceptions import WebhookConfigException
from shared.services.webhook.validator import validate_webhook_url_async

router = APIRouter(tags=["Jobs"])


# ==================== 公共工具函数 ====================


def get_supported_formats() -> str:
    """获取所有支持的文件格式字符串"""
    return ", ".join(sorted(settings.get_supported_extensions()))


async def transition_to_uploaded(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    trigger: str = "manual_upload_completed",
):
    """
    将任务状态转换为uploaded

    Args:
        db: 数据库会话
        job_id: 任务ID
        job_type: 任务类型
        trigger: 触发原因
    """
    state_machine = JobStateMachine()

    # 文件上传完成后，转换到pending状态
    await state_machine.transition(
        db, job_id, JobStatus.PENDING.value, trigger, None, "system"
    )


async def start_workflow_for_job(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    source_type: str,
    user_id: str,
    file_path: Optional[str] = None,
    file_url: Optional[str] = None,
):
    """
    为任务启动工作流

    Args:
        db: 数据库会话
        job_id: 任务ID
        job_type: 任务类型
        source_type: 来源类型
        user_id: 用户ID
        file_path: 文件路径
        file_url: 文件URL
    """
    if job_type == "kb_management":
        orchestrator = KBOrchestrator()
        await orchestrator.start_workflow(
            db=db,
            job_id=job_id,
            source_type=source_type,
            file_path=file_path,
            file_url=file_url,
            user_id=user_id,
        )
    else:
        raise ValidationException(
            user_message="Unsupported job type",
            violations=[{"field": "job_type", "description": f"Job type '{job_type}' is not supported"}]
        )


def check_job_permission(job, user_id: str) -> None:
    """
    检查任务权限

    Args:
        job: 任务对象
        user_id: Current user ID

    Raises:
        HTTPException: 权限不足时抛出异常
    """
    if not job:
        raise NotFoundException(
            resource="Job",
            resource_id=user_id,
            internal_message="Job not found"
        )

    if str(job.user_id) != user_id:
        raise PermissionDeniedException(
            user_message="You don't have permission to access this job",
        )


def _build_error_response(job: Any, job_metadata: Optional[dict] = None) -> Optional[StandardErrorObject]:
    """
    Build StandardErrorObject for embedded error pattern.

    Args:
        job: Job object with job_id, error_code, and error_message
        job_metadata: Job metadata dict that may contain error_details

    Returns:
        StandardErrorObject or None
    """
    if not job.error_message:
        return None

    # Extract error_details from job_metadata if present
    error_details = None
    if job_metadata and isinstance(job_metadata, dict):
        error_details = normalize_error_details(job_metadata.get("error_details"))

    return StandardErrorObject(
        code=job.error_code or "UNKNOWN",
        message=job.error_message,
        request_id=job.job_id,
        details=error_details,
    )


def create_job_response(
    job_id: str,
    job,
    source_type: str,
    data_id: Optional[str],
    upload_url: Optional[str] = None,
    upload_headers: Optional[dict] = None,
    expires_in: Optional[int] = None,
) -> JobResponse:
    """
    创建JobResponse对象

    Args:
        job_id: 任务ID
        job: 任务对象
        source_type: 来源类型
        data_id: 数据ID
        upload_url: 上传URL（仅文件模式）
        upload_headers: 上传头（仅文件模式）
        expires_in: 过期时间（仅文件模式）

    Returns:
        JobResponse: 任务响应对象
    """
    return JobResponse(
        job_id=job_id,
        status=job.status,
        source_type=source_type,
        data_id=data_id,
        created_at=job.created_at,
        upload_url=upload_url,
        upload_headers=upload_headers,
        expires_in=expires_in,
    )


def validate_file_type(file_name: str) -> bool:
    """
    验证文件类型是否支持所有SUPPORTED_EXTENSIONS格式

    Args:
        file_name: 文件名

    Returns:
        bool: 是否支持的文件类型
    """
    if not file_name:
        return False

    file_extension = os.path.splitext(file_name)[1].lower()

    return file_extension in settings.get_supported_extensions()


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """确保返回UTC时间"""
    if not dt:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


@router.post("", response_model=JobResponse, summary="创建解析任务")
@router.post("/", include_in_schema=False)
async def create_job(
    payload: JobCreate,
    http_request: Request,
    current_user: CurrentUser = Depends(require_billing_limits),
    db: AsyncSession = Depends(get_db),
):
    """
    创建解析任务 - 符合PRD第5.1.3节规范
    """
    try:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        # 验证参数
        if payload.source_type == "file" and not payload.file_name:
            raise ValidationException(
                user_message="file_name is required when source_type is 'file'",
                violations=[{"field": "file_name", "description": "Required for file source type"}]
            )
        if payload.source_type == "url" and not payload.source_url:
            raise ValidationException(
                user_message="source_url is required when source_type is 'url'",
                violations=[{"field": "source_url", "description": "Required for url source type"}]
            )

        # Validate webhook config if present
        if payload.webhook:
            # Check for URL validity
            if payload.webhook.url:
                validation_result = await validate_webhook_url_async(payload.webhook.url)
                if not validation_result.is_valid:
                     raise WebhookConfigException(
                        user_message="Invalid webhook URL",
                        internal_message=f"Webhook validation failed: {validation_result.error_message}"
                    )

        # 验证文件类型
        if payload.source_type == "file" and payload.file_name and not validate_file_type(payload.file_name):
            supported_formats = get_supported_formats()
            raise ValidationException(
                user_message=f"Unsupported file type. Supported formats: {supported_formats}",
                violations=[{"field": "file_name", "description": "File type not supported"}]
            )
        elif payload.source_type == "url":
            # Resolve file type from URL path or Content-Type header
            file_ext = await resolve_file_extension_async(payload.source_url)
            if not file_ext:
                supported_formats = get_supported_formats()
                raise ValidationException(
                    user_message=f"Unsupported URL file type. Supported formats: {supported_formats}",
                    violations=[{"field": "source_url", "description": "URL file type not supported"}]
                )

        job_type = "kb_management"

        # 简化版：不再在API层获取user_config，Worker直接使用settings.USERS_DATA_PATH
        from shared.services.redis import RedisServiceFactory
        
        redis_service = RedisServiceFactory.get_service()
        
        # 构建job_metadata（不再包含user_config）
        from shared.models.schemas.job_metadata import JobMetadataHelper
        job_metadata = JobMetadataHelper.create_from_request(payload)

        # Enforce Layers 2-3 immediately before DB insert so the row lock
        # lifetime is limited to capacity check + create_job commit.
        await enforce_job_creation_capacity(
            request=http_request,
            db=db,
            current_user=current_user,
        )

        if payload.source_type == "file":
            # 文件上传模式 - 申请萝卜坑
            assert payload.file_name is not None
            file_extension = os.path.splitext(payload.file_name)[1]
            s3_key = f"uploads/{job_id}{file_extension}"
            job_metadata["source_file_name"] = payload.file_name
            job_metadata["source_type"] = "file"

            # 创建状态为waiting-file的job (s3_key set at creation — single INSERT)
            job_repo = JobRepository()
            job = await job_repo.create_job(
                db=db,
                job_id=job_id,
                user_id=current_user.user_id,
                job_type=job_type,
                source_type="file",
                file_path=None,  # 文件还未上传
                webhook_url=payload.webhook.url if payload.webhook else None,
                metadata=job_metadata,
                initial_state="waiting-file",
                s3_key=s3_key,
            )

            if not job:
                raise JobOperationException(
                    internal_message="Failed to create job in database"
                )

            # 生成预签名URL
            upload_service = FileUploadService()
            upload_info = await upload_service.generate_upload_url(
                job_id, file_extension
            )

            # 3. 保存job_metadata到Redis（2小时缓存）
            from shared.services.redis.job_metadata_service import \
                JobMetadataService
            metadata_service = JobMetadataService(redis_service)
            await metadata_service.save_metadata(job_id, job_metadata)
            
            # 4. 保存Job基本信息到Redis（2小时缓存）
            from datetime import datetime

            from shared.services.redis import JobInfoRedisService
            job_info_service = JobInfoRedisService(redis_service)
            job_info = {
                "job_id": job_id,
                "s3_key": s3_key,
                "user_id": current_user.user_id,
                "webhook_enabled": bool(payload.webhook and payload.webhook.url),
                "job_type": job_type,
                "source_type": "file",
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await job_info_service.save_job_info(job_id, job_info)

            logger.info(f"Job {job_id} upload URL generated for client")

            # 构建响应
            response = create_job_response(
                job_id=job_id,
                job=job,
                source_type="file",
                data_id=payload.data_id,
                upload_url=upload_info["upload_url"],
                upload_headers=upload_info["upload_headers"],
                expires_in=upload_info["expires_in"],
            )

            return response

        else:
            # URL模式 - 创建Job后异步下载和上传
            try:
                # Resolve file extension (URL path first, then Content-Type header)
                file_extension = await resolve_file_extension_async(payload.source_url)
                if not file_extension:
                    supported_formats = get_supported_formats()
                    raise ValidationException(
                        user_message=f"Unsupported URL file type. Supported formats: {supported_formats}",
                        violations=[{"field": "source_url", "description": "URL file type not supported"}]
                    )

                parsed_url = urlparse(payload.source_url)
                url_basename = str(os.path.basename(parsed_url.path))
                # Ensure source_file_name carries the correct extension.
                # URLs like arxiv.org/pdf/1706.03762 have no real extension in the path.
                if url_basename and os.path.splitext(url_basename)[1].lower() == file_extension:
                    source_file_name = url_basename
                elif url_basename:
                    source_file_name = f"{url_basename}{file_extension}"
                else:
                    source_file_name = f"url_file_{uuid.uuid4().hex[:8]}{file_extension}"

                s3_key = f"uploads/{job_id}{file_extension}"
                
                job_metadata.update(
                    {
                        "source_file_name": source_file_name,
                        "source_url": payload.source_url,
                        "source_type": "url",
                    }
                )

                # 创建状态为pending的job（文件将异步上传）
                job_repo = JobRepository()
                job = await job_repo.create_job(
                    db=db,
                    job_id=job_id,
                    user_id=current_user.user_id,
                    job_type=job_type,
                    source_type="url",
                    file_path=None,
                    webhook_url=payload.webhook.url if payload.webhook else None,
                    metadata=job_metadata,
                    initial_state=JobStatus.WAITING_FILE.value,  # 使用pending状态
                    s3_key=s3_key,  # 预设s3_key
                )

                if not job:
                    raise JobOperationException(
                        internal_message="Failed to create URL job in database"
                    )

                # 保存job_metadata到Redis（2小时缓存）
                from shared.services.redis.job_metadata_service import \
                    JobMetadataService
                metadata_service = JobMetadataService(redis_service)
                await metadata_service.save_metadata(job_id, job_metadata)
                
                # 保存Job基本信息到Redis（2小时缓存）
                from datetime import datetime

                from shared.services.redis import JobInfoRedisService
                job_info_service = JobInfoRedisService(redis_service)
                job_info = {
                    "job_id": job_id,
                    "s3_key": s3_key,
                    "user_id": current_user.user_id,
                    "webhook_enabled": bool(payload.webhook and payload.webhook.url),
                    "job_type": job_type,
                    "source_type": "url",
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
                await job_info_service.save_job_info(job_id, job_info)

                # 异步启动URL文件下载和上传任务（任务已迁移到 Worker，通过名称引用）
                from shared.core.celery_app import get_celery_app
                celery_app = get_celery_app()
                upload_url_file_task = celery_app.signature('app.core.tasks.kb_tasks.upload_url_file_task')
                upload_url_file_task.apply_async(
                    args=[job_id, payload.source_url, current_user.user_id],
                    kwargs={
                        "job_type": job_type,
                    }
                )

                # 构建响应
                response = create_job_response(
                    job_id=job_id,
                    job=job,
                    source_type="url",
                    data_id=payload.data_id,
                )

                return response

            except ValidationException:
                raise
            except WebhookConfigException:
                raise
            except (RateLimitException, UnavailableException):
                raise
            except JobOperationException:
                raise
            except Exception as e:
                logger.error(f"URL任务创建失败: {e}")
                raise JobOperationException(
                    internal_message=f"URL job creation failed: {str(e)}"
                )

    except ValidationException:
        raise
    except WebhookConfigException:
        raise
    except (RateLimitException, UnavailableException):
        raise
    except JobOperationException:
        raise
    except Exception as e:
        logger.error(f"创建任务失败: {e}")
        raise JobOperationException(
            internal_message=f"Job creation failed: {str(e)}"
        )


@router.get("/page", response_model=JobList, summary="获取任务列表")
async def list_jobs(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    job_status: Optional[str] = Query(None, description="状态过滤"),
    job_type: Optional[str] = Query(None, description="任务类型过滤"),
    recent_days: Optional[int] = Query(None, description="最近天数过滤，支持 1/7/30", enum=[1, 7, 30]),
    start_time: Optional[datetime] = Query(None, description="开始时间，ISO格式"),
    end_time: Optional[datetime] = Query(None, description="结束时间，ISO格式"),
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取任务列表
    """
    try:
        job_repo = JobRepository()
        
        if recent_days not in (None, 1, 7, 30):
            raise ValidationException(
                user_message="recent_days only supports 1, 7, or 30",
                violations=[{"field": "recent_days", "description": "Invalid value"}]
            )
        created_after = None
        if recent_days:
            from datetime import datetime, timedelta
            created_after = datetime.now() - timedelta(days=recent_days)
        
        if start_time and end_time and start_time > end_time:
            raise ValidationException(
                user_message="start_time cannot be later than end_time",
                violations=[{"field": "start_time", "description": "Must be before end_time"}]
            )
        # start_time / end_time 优先于 recent_days
        if start_time:
            created_after = start_time
        created_before = end_time

        # 获取符合条件的总记录数
        total_count = await job_repo.count_jobs_by_user(
            db=db,
            user_id=current_user.user_id,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )

        # 获取任务列表
        jobs = await job_repo.get_jobs_by_user(
            db=db,
            user_id=current_user.user_id,
            limit=page_size,
            offset=(page - 1) * page_size,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )

        # 构建响应
        job_responses = []
        upload_service = FileUploadService()
        from shared.models.schemas.job_metadata import JobMetadataHelper
        from shared.services.redis import RedisServiceFactory

        redis_service = RedisServiceFactory.get_service()
        for job in jobs:
            # 使用统一接口获取job_metadata
            job_metadata = await job_repo.get_job_metadata(db, job.job_id, redis_service)
            job_result = job.job_result
            status_for_api = job.status

            result_url = None
            result = None
            result_url_expires_at = job.created_at  # 默认使用创建时间

            if job_result and job_result.result_s3_key:
                result_url_info = cast(Dict[str, Any], await upload_service.generate_download_url(
                    job_result.result_s3_key
                ))
                result_url = result_url_info["download_url"]

                # 从 inline_payload 获取 checksum（只包含 checksum）
                if job_result.inline_payload:
                    result = job_result.inline_payload

                # 处理result_url_expires_at字段
                if result_url:
                    from datetime import datetime, timedelta
                    expires_in = int(result_url_info.get("expires_in", 3600))
                    result_url_expires_at = datetime.now() + timedelta(seconds=expires_in)

            original_request = job_metadata.get("original_request") if isinstance(job_metadata, dict) else {}
            source_url = original_request.get("source_url") if isinstance(original_request, dict) else None
            file_name = None
            if source_url:
                parsed_source = urlparse(source_url)
                file_name = os.path.basename(parsed_source.path) or None
            if not file_name and isinstance(original_request, dict):
                file_name = original_request.get("file_name")
            file_extension = None
            if file_name:
                ext = os.path.splitext(file_name)[1]
                file_extension = ext[1:].upper() if ext else None
            
            parsing_params = {}
            if isinstance(original_request, dict):
                parsing_params = original_request.get("parsing_params") or {}
            if not parsing_params and isinstance(job_metadata, dict):
                parsing_params = job_metadata.get("parsing_params") or {}
            model = parsing_params.get("model") if isinstance(parsing_params, dict) else None
            ocr_enabled = parsing_params.get("ocr_enabled") if isinstance(parsing_params, dict) else None
            
            duration_seconds = None
            if job.updated_at and job.created_at:
                duration_seconds = (job.updated_at - job.created_at).total_seconds()
            
            job_responses.append(
                JobResultResponse(
                    job_id=job.job_id,
                    status=status_for_api,
                    source_type=job.source_type,
                    data_id=JobMetadataHelper.get_field(job_metadata, "data_id"),
                    created_at=ensure_utc(job.created_at),
                    progress=None,  # 任务列表不显示详细进度
                    error=_build_error_response(job, job_metadata),
                    result=result,
                    result_url=result_url,
                    result_url_expires_at=ensure_utc(result_url_expires_at),
                    file_name=file_name,
                    file_extension=file_extension,
                    model=model,
                    ocr_enabled=ocr_enabled,
                    duration_seconds=duration_seconds,
                    credits_spent=MicroDollar(job.credits_charged).to_credit() if hasattr(job, "credits_charged") else 0,
                )
            )

        # 计算总页数
        import math
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0

        response = JobList(
            jobs=job_responses, total=total_count, page=page, page_size=page_size, total_pages=total_pages
        )

        return response

    except Exception as e:
        logger.error(f"获取任务列表失败: {e}")
        raise JobOperationException(
            internal_message=f"Failed to get job list: {str(e)}"
        )


@router.get(
    "/{job_id}", response_model=JobResultResponse, summary="获取任务结果"
)
async def get_job_result(
    job_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取任务结果 - 符合PRD第5.1.3节规范
    """
    try:
        job_repo = JobRepository()

        # 获取Job并检查权限
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, current_user.user_id)
        assert job is not None

        status_for_api = job.status

        # 获取进度信息，当任务状态为running时，从Redis获取详细进度信息
        progress = None
        if status_for_api == "running":
            # TODO：从Redis获取详细进度信息，并转换为progress格式
            # from shared.services.redis import RedisServiceFactory
            # redis_service = RedisServiceFactory.get_service()
            # from shared.utils.redis_key_builder import redis_key_builder

            # progress_key = redis_key_builder.task_progress(job_id)
            # progress = await redis_service.hgetall(progress_key)
            progress = {"total_pages": 10, "processed_pages": 5}

        # 使用统一接口获取job_metadata
        from shared.models.schemas.job_metadata import JobMetadataHelper
        from shared.services.redis import RedisServiceFactory
        
        redis_service = RedisServiceFactory.get_service()
        job_metadata = await job_repo.get_job_metadata(db, job_id, redis_service)

        # 结果交付信息
        job_result = job.job_result
        result_url = None
        result = None
        result_url_expires_at = job.created_at  # 默认使用创建时间
        
        if job_result and job_result.result_s3_key:
            upload_service = FileUploadService()
            result_url_info = cast(Dict[str, Any], await upload_service.generate_download_url(
                job_result.result_s3_key
            ))
            result_url = result_url_info["download_url"]
            expires_in = int(result_url_info["expires_in"])

            # 从 inline_payload 获取 checksum 和 statistics
            if job_result.inline_payload:
                result = job_result.inline_payload

            # 处理result_url_expires_at字段
            if result_url:
                from datetime import datetime, timedelta
                result_url_expires_at = datetime.now() + timedelta(seconds=expires_in)

        original_request = job_metadata.get("original_request") if isinstance(job_metadata, dict) else {}
        source_url = original_request.get("source_url") if isinstance(original_request, dict) else None
        file_name = None
        if source_url:
            parsed_source = urlparse(source_url)
            file_name = os.path.basename(parsed_source.path) or None
        if not file_name and isinstance(original_request, dict):
            file_name = original_request.get("file_name")
        file_extension = None
        if file_name:
            ext = os.path.splitext(file_name)[1]
            file_extension = ext[1:].upper() if ext else None
        
        parsing_params = {}
        if isinstance(original_request, dict):
            parsing_params = original_request.get("parsing_params") or {}
        if not parsing_params and isinstance(job_metadata, dict):
            parsing_params = job_metadata.get("parsing_params") or {}
        model = parsing_params.get("model") if isinstance(parsing_params, dict) else None
        ocr_enabled = parsing_params.get("ocr_enabled") if isinstance(parsing_params, dict) else None
        
        response_data = JobResultResponse(
            job_id=job.job_id,
            status=status_for_api,
            source_type=job.source_type,
            data_id=JobMetadataHelper.get_field(job_metadata, "data_id"),
            created_at=ensure_utc(job.created_at),
            progress=progress,
            error=_build_error_response(job, job_metadata),
            result=result,
            result_url=result_url,
            result_url_expires_at=ensure_utc(result_url_expires_at),
            file_name=file_name,
            file_extension=file_extension,
            model=model,
            ocr_enabled=ocr_enabled,
            duration_seconds=(job.updated_at - job.created_at).total_seconds() if job.updated_at and job.created_at else None,
            credits_spent=MicroDollar(job.credits_charged).to_credit() if hasattr(job, "credits_charged") else 0,
        )

        return response_data

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except Exception as e:
        logger.error(f"获取任务结果失败: {e}")
        raise JobOperationException(
            internal_message=f"Failed to get job result: {str(e)}"
        )


@router.post(
    "/{job_id}/confirm-upload",
    response_model=dict,
    summary="确认文件上传",
)
async def confirm_upload(
    job_id: str,
    request: Optional[ConfirmUploadRequest] = None,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    确认文件上传完成 - 备用机制
    """
    try:
        job_repo = JobRepository()

        # 获取Job并检查权限
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, current_user.user_id)

        # 检查任务状态
        logger.info(f"Confirm upload - Job {job_id} current status: {job.status}")
        if job.status not in [JobStatus.PENDING.value, JobStatus.WAITING_FILE.value]:
            # 如果已经被webhook触发，返回成功（幂等性）
            logger.info(f"Job {job_id} already processed, status: {job.status}")
            return {"message": "任务状态已更新"}

        # 验证S3文件存在
        if not job.s3_key:
            raise ValidationException(
                user_message="Job is missing S3 key information",
                violations=[{"field": "s3_key", "description": "S3 key not set for this job"}]
            )

        upload_service = FileUploadService()
        file_info = await upload_service.verify_s3_file_exists(job.s3_key)

        if not file_info.get("exists"):
            raise ValidationException(
                user_message="S3 file does not exist, please upload the file first",
                violations=[{"field": "file", "description": "File not found in S3"}]
            )

        # 更新任务状态
        await transition_to_uploaded(
            db, job_id, job.job_type, "manual_upload_completed"
        )

        # 触发任务处理
        await start_workflow_for_job(
            db=db,
            job_id=job_id,
            job_type=job.job_type,
            source_type="file",
            user_id=current_user.user_id,
        )

        return {"message": "文件上传确认成功，任务已开始处理"}

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"确认上传失败: {e}")
        raise JobOperationException(
            internal_message=f"Failed to confirm upload: {str(e)}"
        )
