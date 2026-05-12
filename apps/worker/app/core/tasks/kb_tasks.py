"""
Knowledge Base Management Celery Tasks

Sync implementation for gevent worker pool.
All I/O operations use sync services that yield cooperatively under gevent.
"""

import os
from datetime import datetime, timezone

import pandas as pd

# Base task class
from app.core.tasks.base_task import KBBaseTask
from app.core.tasks.task_utils import (
    cleanup_task_workspace,
    create_task_workspace,
    download_s3_file_to_temp,
)
from app.services.common.job_start_service import mark_job_running
from app.services.document_parser.stage_profiler import stage_timer

# Storage operations
from app.services.storage.sync_storage_service import (
    download_file_from_url,
    generate_download_url,
    upload_to_s3,
    verify_s3_file_exists,
)
from app.services.workload.page_estimator import PageEstimator
from loguru import logger
from sqlalchemy import select

from shared.core.celery_app import get_celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions import RETRYABLE_EXCEPTIONS

# Exception handling
from shared.core.exceptions.domain_exceptions import (
    InsufficientCreditsException,
    NotFoundException,
    StorageServiceException,
    ValidationException,
    WorkerHandlingException,
)
from shared.core.logging import LogEvent, log_context
from shared.models.database.job import Job

# Domain services
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.billing.work_billing_service import WorkBillingService
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.job_lifecycle_sync import get_sync_job_lifecycle_service
from shared.services.redis.distributed_lock import RedisJobLock

# Sync services for gevent worker
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
    SyncRedisServiceFactory,
)
from shared.services.storage.result_storage import get_result_storage
from shared.services.storage.zip_result_service import ZipResultService
from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
    load_nav_top_summary,
)

# Get Celery application
celery_app = get_celery_app()



@celery_app.task(
    bind=True,
    base=KBBaseTask,
    name="app.core.tasks.kb_tasks.upload_url_file_task",
    ignore_result=True,
    autoretry_for=RETRYABLE_EXCEPTIONS,
    retry_kwargs={
        "countdown": settings.KB_TASK_RETRY_COUNTDOWN,
        "max_retries": settings.KB_TASK_MAX_RETRIES,
    },
)
def upload_url_file_task(
    self,
    job_id: str,
    source_url: str,
    user_id: str | None = None,
    job_type: str | None = None,
):
    """Download file from URL and upload to S3."""
    with log_context(task_id=self.request.id):
        if not job_id:
            raise WorkerHandlingException(
                user_message="An unexpected system error occurred",
                internal_message="Worker task 'upload_url_file_task' called without job_id",
            )

        result = _upload_url_file(job_id, source_url, user_id, job_type)

        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: upload_url_file_task"
        )
        return result


def _upload_url_file(
    job_id: str, source_url: str, user_id: str | None, job_type: str | None = None
):
    """Sync URL file download and upload to S3."""
    lifecycle_service = get_sync_job_lifecycle_service()

    # Get job info from Redis
    redis_service = SyncRedisServiceFactory.get_service()
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if not job_info:
        metadata_service = SyncJobMetadataService(redis_service)
        job_metadata = metadata_service.get_metadata(job_id)
        if job_metadata:
            s3_key = job_metadata.get("s3_key")
        else:
            raise NotFoundException(
                resource="JobInfo",
                resource_id=job_id,
                internal_message="Job info not found in Redis or Metadata",
            )
    else:
        s3_key = job_info.get("s3_key")

    if not s3_key:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="s3_key",
            internal_message=f"Missing s3_key in Redis job info for job_id={job_id}",
        )

    # Publish progress: validating file type
    lifecycle_service.update_progress(
        job_id, progress=3, message="Validating URL file type..."
    )

    # Step 1: Validate URL file type (path first, then Content-Type header)
    from shared.utils.url_file_type import resolve_file_extension_sync

    file_extension = resolve_file_extension_sync(source_url)

    if not file_extension:
        all_supported_extensions = settings.get_supported_extensions()
        supported_formats = ", ".join(sorted(all_supported_extensions))
        raise ValidationException(
            user_message="Unsupported file type",
            violations=[
                {
                    "field": "file_extension",
                    "description": f"Must be one of: {supported_formats}",
                }
            ],
        )

    # Publish progress: downloading
    lifecycle_service.update_progress(
        job_id, progress=10, message="Downloading file from URL..."
    )

    # Step 2: Download file to temp directory
    try:
        temp_file_path = download_file_from_url(source_url)
    except Exception as e:
        raise ValidationException(
            user_message="Failed to download file from URL",
            violations=[
                {
                    "field": "source_url",
                    "description": "Could not download file from the provided URL",
                }
            ],
            internal_message=f"Failed to download file from URL: {source_url}, error: {e}",
        )

    try:
        # Publish progress: validating file size
        lifecycle_service.update_progress(
            job_id, progress=30, message="Validating file size..."
        )

        # Step 3: Validate file size
        file_size = os.path.getsize(temp_file_path)

        if file_size > settings.MAX_FILE_SIZE:
            limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
            raise ValidationException(
                user_message=f"File size exceeds limit (max {limit_mb}MB for {file_extension})",
                violations=[
                    {
                        "field": "file_size",
                        "description": f"Size {file_size} bytes exceeds limit of {settings.MAX_FILE_SIZE} bytes",
                    }
                ],
            )

        # Publish progress: uploading to S3
        lifecycle_service.update_progress(
            job_id, progress=50, message="Uploading file to S3..."
        )

        # Step 4: Upload to S3
        uploads_bucket = settings.S3_BUCKET_NAME
        upload_to_s3(temp_file_path, s3_key, uploads_bucket)
        logger.info(f"File uploaded to S3: {s3_key}")

    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            logger.debug(f"Temp file cleaned up: {temp_file_path}")

    # Publish progress: verifying upload
    lifecycle_service.update_progress(
        job_id, progress=80, message="Verifying upload result..."
    )

    # Step 5: Verify S3 file exists
    file_info = verify_s3_file_exists(s3_key)
    if not file_info.get("exists"):
        raise StorageServiceException(
            user_message="We failed to verify your file upload",
            internal_message=f"S3 file verification failed for {s3_key}",
        )

    # Publish progress: complete
    lifecycle_service.update_progress(
        job_id,
        progress=100,
        message="URL file upload complete, waiting for processing...",
    )

    logger.info(
        f"URL file upload complete, waiting for S3 webhook: {job_id} -> {s3_key}"
    )

    return {
        "status": "success",
        "job_id": job_id,
        "s3_key": s3_key,
        "file_size": file_info.get("size"),
    }


@celery_app.task(
    bind=True,
    base=KBBaseTask,
    name="app.core.tasks.kb_tasks.parse_task",
    ignore_result=True,
    autoretry_for=RETRYABLE_EXCEPTIONS,
    retry_kwargs={
        "countdown": settings.KB_TASK_RETRY_COUNTDOWN,
        "max_retries": settings.KB_TASK_MAX_RETRIES,
    },
)
def parse_task(
    self, job_id: str, user_id: str | None = None, job_type: str = "kb_management"
):
    """Parse and vectorize task (file already uploaded to S3)."""
    with log_context(task_id=self.request.id):
        if not job_id:
            raise WorkerHandlingException(
                user_message="An unexpected system error occurred",
                internal_message="Worker task 'parse_task' called without job_id",
            )

        result = _parse(job_id, user_id)

        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: parse_task"
        )
        return result


def _parse(job_id: str, user_id: str | None):
    """Sync parse and vectorize (file already uploaded to S3)."""
    logger.info(f"Parse started: job_id={job_id}, user_id={user_id}")
    lifecycle_service = get_sync_job_lifecycle_service()

    # Get job info from Redis (sync)
    redis_service = SyncRedisServiceFactory.get_service()
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if not job_info:
        # Redis JobInfo has expired or been flushed — fall back to the DB, which is
        # the durable source of truth for s3_key and user_id written at job creation.
        logger.warning(
            f"JobInfo not found in Redis for job_id={job_id}; falling back to database"
        )
        with get_sync_db_context() as fallback_db:
            job_row = fallback_db.execute(
                select(Job).where(Job.job_id == job_id)
            ).scalar_one_or_none()

        if not job_row or not job_row.s3_key:
            raise NotFoundException(
                resource="JobInfo",
                resource_id=job_id,
                internal_message="job info not found in Redis or database",
            )

        s3_key: str = job_row.s3_key
        job_user_id: str | None = str(job_row.user_id) if job_row.user_id else user_id
        logger.info(
            f"Recovered JobInfo from database: job_id={job_id}, s3_key={s3_key}"
        )
    else:
        raw_s3_key = job_info.get("s3_key")
        if not isinstance(raw_s3_key, str) or not raw_s3_key:
            raise NotFoundException(
                resource="JobInfo",
                resource_id="s3_key",
                internal_message="Missing s3_key in job_info",
            )

        s3_key = raw_s3_key
        raw_job_user_id = job_info.get("user_id")
        job_user_id = raw_job_user_id if isinstance(raw_job_user_id, str) else user_id

    # Verify S3 file exists (sync)
    file_info = verify_s3_file_exists(s3_key)
    if not file_info.get("exists"):
        raise NotFoundException(
            resource="S3File",
            resource_id=s3_key,
            internal_message=f"S3 file not found: {s3_key}",
        )

    logger.info(f"S3 file verified: {s3_key}")

    # Validate file size
    file_size = file_info.get("size", 0)
    file_extension = os.path.splitext(s3_key)[1].lower()

    if file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        raise ValidationException(
            user_message=f"File size exceeds limit (max {limit_mb}MB for {file_extension})",
            violations=[
                {
                    "field": "file_size",
                    "description": f"Size {file_size} bytes exceeds limit of {settings.MAX_FILE_SIZE} bytes",
                }
            ],
        )

    # Get job_metadata from Redis
    metadata_service = SyncJobMetadataService(redis_service)
    job_metadata = metadata_service.get_metadata(job_id)
    if not job_metadata:
        raise NotFoundException(
            resource="JobMetadata",
            resource_id=job_id,
            internal_message=f"Job metadata not found for job_id={job_id}",
        )

    should_process = mark_job_running(job_id, redis_service)
    if not should_process:
        logger.warning(f"Skipping parse_task for inactive job: job_id={job_id}")
        return {
            "status": "skipped",
            "job_id": job_id,
            "reason": "job_already_terminal",
        }

    # Acquire distributed lock to prevent concurrent processing of the same
    # job when the broker redelivers a task before the original worker acks.
    # If another worker already holds the lock, UnavailableException is raised
    # and Celery auto-retries after KB_TASK_RETRY_COUNTDOWN seconds.
    with RedisJobLock(redis_service, job_id):
        task_workspace_dir = create_task_workspace(job_id)
        input_dir = os.path.join(task_workspace_dir, "input")
        output_dir = os.path.join(task_workspace_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        logger.info(
            f"Task workspace ready: job_id={job_id}, workspace={task_workspace_dir}"
        )

        try:
            # Publish progress: start parsing
            lifecycle_service.update_progress(
                job_id, progress=10, message="Parsing document..."
            )

            # Generate download URL and download file (sync)
            file_url_response = generate_download_url(s3_key, settings.S3_BUCKET_NAME)
            file_url = file_url_response["download_url"]

            filename = JobMetadataHelper.get_field(job_metadata, "source_file_name")

            # Download file to the task workspace
            page_count = 1

            # Derive file extension from s3_key (always has the correct extension)
            # rather than filename, which may not have a real extension for URLs
            # like arxiv.org/pdf/1706.03762
            file_ext = os.path.splitext(s3_key)[1].lower() if s3_key else ""
            local_temp_path = download_s3_file_to_temp(file_url, file_ext, input_dir)

            logger.info(
                f"File downloaded: job_id={job_id}, local_path={local_temp_path}"
            )

            from app.services.document_parser.internal_parse_name import (
                prepare_internal_parse_input,
            )
            from app.services.document_parser.parse_service import (
                checkerboard_inject_parse,
            )

            prepared_parse_input = prepare_internal_parse_input(
                local_temp_path,
                filename,
                fallback_ext=file_ext,
                prefer_fallback_ext=True,
            )
            internal_parse_name = prepared_parse_input.internal_filename
            local_temp_path = prepared_parse_input.file_path
            logger.info(
                f"File prepared for parsing: job_id={job_id}, "
                f"internal_filename={internal_parse_name}, local_path={local_temp_path}"
            )

            # Estimate workload
            page_count = PageEstimator.estimate(local_temp_path)
            logger.info(
                f"Workload estimation: job_id={job_id}, page_count={page_count}"
            )

            processing_started_at = datetime.now(timezone.utc)

            if not job_user_id:
                raise NotFoundException(
                    resource="JobInfo",
                    resource_id="user_id",
                    internal_message=f"Missing user_id in job info for job_id={job_id}",
                )

            billing_service = WorkBillingService()
            billing_status = "skipped"
            billing_amount_micro_dollars = 0
            billing_credits = 0.0
            with get_sync_db_context() as db:
                job_result = db.execute(
                    select(Job).where(Job.job_id == job_id).with_for_update()
                )
                job = job_result.scalar_one_or_none()

                if job and getattr(job, "billing_status", "") == "charged":
                    logger.info(f"Job already charged: {job_id}")
                    billing_status = "charged"
                    billing_amount_micro_dollars = int(job.credits_charged or 0)
                    billing_credits = billing_amount_micro_dollars / 1_000_000
                else:
                    try:
                        billing_result = billing_service.charge_for_pages(
                            session=db,
                            user_id=job_user_id,
                            page_count=page_count,
                            filename=filename,
                        )
                    except InsufficientCreditsException:
                        logger.warning(
                            f"Billing failed: job_id={job_id}, user_id={job_user_id}"
                        )
                        billing_amount = billing_service.estimate_page_charge(
                            page_count=page_count
                        )
                        if job:
                            job.page_count = page_count
                            job.credits_charged = billing_amount.amount_micro_dollars
                            job.billing_status = "billing_failed"
                            db.commit()

                        raise InsufficientCreditsException(
                            user_message=(
                                "Insufficient credits to process this document "
                                f"({page_count} pages required, cost: "
                                f"{billing_amount.credits})."
                            ),
                            required_credits=billing_amount.credits,
                            internal_message=(
                                f"job_id={job_id}, user_id={job_user_id}, "
                                f"page_count={page_count}"
                            ),
                        )

                    billing_status = billing_result.billing_status
                    billing_amount_micro_dollars = billing_result.amount_micro_dollars
                    billing_credits = billing_result.credits
                    if job:
                        job.page_count = page_count
                        job.credits_charged = billing_amount_micro_dollars
                        job.billing_status = billing_status

            # Store billing info in Redis
            metadata_updates = {
                "page_count": page_count,
                "billing_status": billing_status,
                "billing_amount_micro_dollars": billing_amount_micro_dollars,
                "billing_credits": billing_credits,
                "processing_started_at": processing_started_at.isoformat(),
            }
            metadata_service.update_metadata(job_id, metadata_updates)
            job_metadata.update(metadata_updates)

            doc_type = JobMetadataHelper.get_parsing_param(
                job_metadata, "doc_type", "auto"
            )
            logger.info(
                f"Start parse: job_id={job_id}, filename={filename}, "
                f"internal_filename={internal_parse_name}, type={doc_type}"
            )

            with stage_timer(
                "worker.parse.document",
                job_id=job_id,
                filename=filename,
                doc_type=doc_type,
            ):
                add_dir, add_contents_df = checkerboard_inject_parse(
                    file_full_path=local_temp_path,
                    filename=filename,
                    output_dir=output_dir,
                    job_id=job_id,
                    internal_output_filename=internal_parse_name,
                    kb_dir=JobMetadataHelper.get_parsing_param(
                        job_metadata, "kb_dir", "Default_Root"
                    ),
                    doc_type=doc_type,
                    smart_title_parse=JobMetadataHelper.get_parsing_param(
                        job_metadata, "smart_title_parse", True
                    ),
                    summary_image=JobMetadataHelper.get_parsing_param(
                        job_metadata, "summary_image", True
                    ),
                    summary_table=JobMetadataHelper.get_parsing_param(
                        job_metadata, "summary_table", True
                    ),
                    summary_txt=JobMetadataHelper.get_parsing_param(
                        job_metadata, "summary_txt", True
                    ),
                    add_frag_desc=JobMetadataHelper.get_parsing_param(
                        job_metadata, "add_frag_desc", ""
                    ),
                    s3_key=s3_key,
                )
                parsed_contents_df: pd.DataFrame | None = add_contents_df

            logger.info(
                f"File parsing completed: job_id={job_id}, add_dir={add_dir}, chunks={len(parsed_contents_df) if parsed_contents_df is not None else 0}"
            )

            if parsed_contents_df is None:
                raise WorkerHandlingException(
                    user_message="We could not extract content from your file",
                    internal_message="File parsing failed, no content returned from parser",
                )

            if parsed_contents_df.empty:
                logger.warning(
                    f"No content returned from file parsing: job_id={job_id}, filename={filename}"
                )

            lifecycle_service.update_progress(
                job_id, progress=30, message="Parse completed, preparing chunks..."
            )

            chunks = dataframe_to_chunks(parsed_contents_df)

            lifecycle_service.update_progress(
                job_id, progress=70, message="Chunks ready, generating zip..."
            )
            logger.info(f"Chunks prepared: job_id={job_id}, count={len(chunks)}")

            # Get source file name
            source_file_name = JobMetadataHelper.get_field(
                job_metadata, "source_file_name"
            ) or JobMetadataHelper.get_field(job_metadata, "source_url")
            if isinstance(source_file_name, str) and "/" in source_file_name:
                source_file_name = os.path.basename(source_file_name)

            document_top_summary = ""
            section_summaries: dict[str, str] = {}
            if add_dir and source_file_name:
                if add_contents_df is not None and "path" in add_contents_df.columns:
                    ensure_doc_nav_json(
                        str(add_dir),
                        chunks,
                        source_file_name=str(source_file_name),
                    )
                # Enrich non-leaf section summaries (bottom-up aggregation)
                try:
                    kb_dir_for_enrich = os.path.dirname(str(add_dir))
                    summary_use_llm = JobMetadataHelper.get_parsing_param(
                        job_metadata, "summary_use_llm", False
                    )
                    enrich_doc_nav_summaries(
                        kb_dir_for_enrich,
                        source_file=str(source_file_name),
                        use_llm=summary_use_llm,
                    )
                    section_summaries = build_section_summary_lookup(str(add_dir))
                except Exception as _e:
                    logger.warning(f"doc_nav enrichment failed (non-fatal): {_e}")
                document_top_summary = load_nav_top_summary(str(add_dir), str(source_file_name))
            if document_top_summary:
                for chunk in chunks:
                    metadata = chunk.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                        chunk["metadata"] = metadata
                    metadata["document_top_summary"] = document_top_summary

            data_id = JobMetadataHelper.get_field(job_metadata, "data_id")

            lifecycle_service.update_progress(
                job_id, progress=80, message="Generating ZIP package..."
            )
            processing_completed_at = datetime.now(timezone.utc)
            processing_timing_updates = {
                "processing_completed_at": processing_completed_at.isoformat(),
                "processing_duration_ms": max(
                    0,
                    int(
                        (
                            processing_completed_at - processing_started_at
                        ).total_seconds()
                        * 1000
                    ),
                ),
            }
            metadata_service.update_metadata(job_id, processing_timing_updates)
            job_metadata.update(processing_timing_updates)

            # Generate ZIP package
            zip_service = ZipResultService()
            zip_file_path, checksum, statistics, zip_size = (
                zip_service.generate_zip_package(
                    job_id=job_id,
                    chunks=chunks,
                    add_dir=str(add_dir) if add_dir else "",
                    source_file_name=source_file_name,
                    data_id=data_id,
                    job_metadata=job_metadata,
                    parsed_df=parsed_contents_df,
                    temp_dir=task_workspace_dir,
                )
            )

            checksum_value = (
                checksum.get("value", "")
                if isinstance(checksum, dict)
                else (checksum or "")
            )

            lifecycle_service.update_progress(
                job_id, progress=90, message="Uploading results to S3..."
            )

            result_bundle = get_result_storage().upload(
                job_id=job_id,
                result_dir=str(add_dir) if add_dir else "",
                zip_file_path=zip_file_path,
            )
            result_s3_key = result_bundle.zip_key

            stored_count = 0

            lifecycle_service.update_progress(
                job_id, progress=100, message="Task complete!"
            )

            # Finalize job success directly to the database
            lifecycle_service.finalize_job_success(
                job_id=job_id,
                chunks=chunks,
                result_s3_key=result_s3_key,
                checksum=checksum_value,
                zip_size=zip_size,
                stored_count=stored_count,
                delivery_mode="url",
                section_summaries=section_summaries,
            )

            logger.info(
                f"Worker processing complete: job_id={job_id}, result_s3_key={result_s3_key}"
            )

            return {
                "status": "success",
                "job_id": job_id,
                "add_dir": None,
                "vectors_count": 0,
                "contents_count": len(parsed_contents_df),
                "stored_count": stored_count,
                "delivery_mode": "url",
                "result_s3_key": result_s3_key,
            }
        finally:
            cleanup_task_workspace(task_workspace_dir)
