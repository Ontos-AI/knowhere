from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
    load_nav_top_summary,
)
from app.services.document_ingestion.job_state_gate import mark_job_running
from app.services.document_ingestion.page_estimator import PageEstimator
from app.services.document_ingestion.workspace import (
    cleanup_task_workspace,
    create_task_workspace,
    download_s3_file_to_temp,
)
from app.services.document_parser.stage_profiler import stage_timer
from app.services.storage.sync_storage_service import verify_s3_file_exists
from loguru import logger
from sqlalchemy import select

from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    InsufficientCreditsException,
    NotFoundException,
    ValidationException,
    WorkerHandlingException,
)
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.billing.work_billing_service import WorkBillingService
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
from shared.services.job_lifecycle_sync import get_sync_job_lifecycle_service
from shared.services.redis.distributed_lock import RedisJobLock
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
    SyncRedisServiceFactory,
)
from shared.services.storage.result_storage import get_result_storage
from shared.services.storage.zip_result_service import ZipResultService


@dataclass(frozen=True)
class _ParseJobContext:
    job_metadata: dict[str, object]
    job_user_id: str | None
    metadata_service: SyncJobMetadataService
    redis_service: Any
    s3_key: str


@dataclass(frozen=True)
class _ParseJobBillingSnapshot:
    billing_amount_micro_dollars: int
    billing_credits: float
    billing_status: str


def parse_uploaded_file_job(job_id: str, user_id: str | None) -> dict[str, object]:
    """Run worker-side Document Ingestion for an uploaded file Job."""
    logger.info(f"Parse started: job_id={job_id}, user_id={user_id}")
    lifecycle_service = get_sync_job_lifecycle_service()

    redis_service = SyncRedisServiceFactory.get_service()
    job_context = _load_parse_job_context(job_id, user_id, redis_service)
    _assert_source_file_within_size_limit(job_context.s3_key)

    should_process = mark_job_running(job_id, job_context.redis_service)
    if not should_process:
        logger.warning(f"Skipping parse_task for inactive job: job_id={job_id}")
        return {
            "status": "skipped",
            "job_id": job_id,
            "reason": "job_already_terminal",
        }

    with RedisJobLock(job_context.redis_service, job_id):
        task_workspace_dir, input_dir, output_dir = _prepare_task_workspace(job_id)
        try:
            return _run_parse_job(
                job_id=job_id,
                job_context=job_context,
                lifecycle_service=lifecycle_service,
                input_dir=input_dir,
                output_dir=output_dir,
                task_workspace_dir=task_workspace_dir,
            )
        finally:
            cleanup_task_workspace(task_workspace_dir)

    raise WorkerHandlingException(
        user_message="We could not complete document processing",
        internal_message=f"Parse workflow exited without a result for job_id={job_id}",
    )


def _load_parse_job_context(
    job_id: str,
    requested_user_id: str | None,
    redis_service: Any,
) -> _ParseJobContext:
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if not job_info:
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
        job_user_id: str | None = (
            str(job_row.user_id) if job_row.user_id else requested_user_id
        )
        logger.info(f"Recovered JobInfo from database: job_id={job_id}, s3_key={s3_key}")
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
        job_user_id = (
            raw_job_user_id if isinstance(raw_job_user_id, str) else requested_user_id
        )

    metadata_service = SyncJobMetadataService(redis_service)
    raw_job_metadata = metadata_service.get_metadata(job_id)
    if not isinstance(raw_job_metadata, dict) or not raw_job_metadata:
        raise NotFoundException(
            resource="JobMetadata",
            resource_id=job_id,
            internal_message=f"Job metadata not found for job_id={job_id}",
        )

    return _ParseJobContext(
        job_metadata=dict(raw_job_metadata),
        job_user_id=job_user_id,
        metadata_service=metadata_service,
        redis_service=redis_service,
        s3_key=s3_key,
    )


def _assert_source_file_within_size_limit(s3_key: str) -> None:
    file_info = verify_s3_file_exists(s3_key)
    if not file_info.get("exists"):
        raise NotFoundException(
            resource="S3File",
            resource_id=s3_key,
            internal_message=f"S3 file not found: {s3_key}",
        )

    logger.info(f"S3 file verified: {s3_key}")

    file_size = file_info.get("size", 0)
    file_extension = os.path.splitext(s3_key)[1].lower()
    if file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        raise ValidationException(
            user_message=f"File size exceeds limit (max {limit_mb}MB for {file_extension})",
            violations=[
                {
                    "field": "file_size",
                    "description": (
                        f"Size {file_size} bytes exceeds limit of "
                        f"{settings.MAX_FILE_SIZE} bytes"
                    ),
                }
            ],
        )


def _prepare_task_workspace(job_id: str) -> tuple[str, str, str]:
    task_workspace_dir = create_task_workspace(job_id)
    input_dir = os.path.join(task_workspace_dir, "input")
    output_dir = os.path.join(task_workspace_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    logger.info(
        f"Task workspace ready: job_id={job_id}, workspace={task_workspace_dir}"
    )
    return task_workspace_dir, input_dir, output_dir


def _run_parse_job(
    *,
    job_id: str,
    job_context: _ParseJobContext,
    lifecycle_service: Any,
    input_dir: str,
    output_dir: str,
    task_workspace_dir: str,
) -> dict[str, object]:
    lifecycle_service.update_progress(job_id, progress=10, message="Parsing document...")

    filename = JobMetadataHelper.get_field(job_context.job_metadata, "source_file_name")
    file_ext = os.path.splitext(job_context.s3_key)[1].lower() if job_context.s3_key else ""
    local_temp_path = download_s3_file_to_temp(job_context.s3_key, file_ext, input_dir)
    logger.info(f"File downloaded: job_id={job_id}, local_path={local_temp_path}")

    from app.services.document_parser.internal_parse_name import (
        prepare_internal_parse_input,
    )
    from app.services.document_parser import parse_service

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

    page_count = PageEstimator.estimate(local_temp_path)
    logger.info(f"Workload estimation: job_id={job_id}, page_count={page_count}")

    processing_started_at = datetime.now(timezone.utc)
    billing_snapshot = _charge_parse_job_pages(
        job_id=job_id,
        filename=filename,
        job_user_id=job_context.job_user_id,
        page_count=page_count,
    )
    _record_processing_start(
        job_id=job_id,
        job_context=job_context,
        billing_snapshot=billing_snapshot,
        page_count=page_count,
        processing_started_at=processing_started_at,
    )

    doc_type = JobMetadataHelper.get_parsing_param(
        job_context.job_metadata,
        "doc_type",
        "auto",
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
        add_dir, parsed_contents_df = parse_service.checkerboard_inject_parse(
            file_full_path=local_temp_path,
            filename=filename,
            output_dir=output_dir,
            job_id=job_id,
            internal_output_filename=internal_parse_name,
            kb_dir=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "kb_dir",
                "Default_Root",
            ),
            doc_type=doc_type,
            smart_title_parse=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "smart_title_parse",
                True,
            ),
            summary_image=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_image",
                True,
            ),
            summary_table=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_table",
                True,
            ),
            summary_txt=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_txt",
                True,
            ),
            add_frag_desc=JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "add_frag_desc",
                "",
            ),
            s3_key=job_context.s3_key,
        )

    logger.info(
        "File parsing completed: "
        f"job_id={job_id}, add_dir={add_dir}, "
        f"chunks={len(parsed_contents_df) if parsed_contents_df is not None else 0}"
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
        job_id,
        progress=30,
        message="Parse completed, preparing chunks...",
    )
    chunks = dataframe_to_chunks(parsed_contents_df)

    lifecycle_service.update_progress(
        job_id,
        progress=70,
        message="Chunks ready, generating zip...",
    )
    logger.info(f"Chunks prepared: job_id={job_id}, count={len(chunks)}")

    return _finalize_parse_job_success(
        add_dir=add_dir,
        chunks=chunks,
        job_context=job_context,
        job_id=job_id,
        lifecycle_service=lifecycle_service,
        parsed_contents_df=parsed_contents_df,
        processing_started_at=processing_started_at,
        task_workspace_dir=task_workspace_dir,
    )


def _charge_parse_job_pages(
    *,
    job_id: str,
    filename: str | None,
    job_user_id: str | None,
    page_count: int,
) -> _ParseJobBillingSnapshot:
    if not job_user_id:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="user_id",
            internal_message=f"Missing user_id in job info for job_id={job_id}",
        )

    billing_service = WorkBillingService()
    billing_filename = filename or ""
    billing_status = "skipped"
    billing_amount_micro_dollars = 0
    billing_credits = 0.0

    with get_sync_db_context() as db:
        job_result = db.execute(select(Job).where(Job.job_id == job_id).with_for_update())
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
                    filename=billing_filename,
                )
            except InsufficientCreditsException:
                logger.warning(f"Billing failed: job_id={job_id}, user_id={job_user_id}")
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

    return _ParseJobBillingSnapshot(
        billing_amount_micro_dollars=billing_amount_micro_dollars,
        billing_credits=billing_credits,
        billing_status=billing_status,
    )


def _record_processing_start(
    *,
    job_id: str,
    job_context: _ParseJobContext,
    billing_snapshot: _ParseJobBillingSnapshot,
    page_count: int,
    processing_started_at: datetime,
) -> None:
    metadata_updates = {
        "page_count": page_count,
        "billing_status": billing_snapshot.billing_status,
        "billing_amount_micro_dollars": billing_snapshot.billing_amount_micro_dollars,
        "billing_credits": billing_snapshot.billing_credits,
        "processing_started_at": processing_started_at.isoformat(),
    }
    job_context.metadata_service.update_metadata(job_id, metadata_updates)
    job_context.job_metadata.update(metadata_updates)


def _finalize_parse_job_success(
    *,
    add_dir: str,
    chunks: list[dict[str, Any]],
    job_context: _ParseJobContext,
    job_id: str,
    lifecycle_service: Any,
    parsed_contents_df: pd.DataFrame,
    processing_started_at: datetime,
    task_workspace_dir: str,
) -> dict[str, object]:
    source_file_name = JobMetadataHelper.get_field(
        job_context.job_metadata,
        "source_file_name",
    ) or JobMetadataHelper.get_field(job_context.job_metadata, "source_url")
    if isinstance(source_file_name, str) and "/" in source_file_name:
        source_file_name = os.path.basename(source_file_name)

    document_top_summary = ""
    section_summaries: dict[str, str] = {}
    if add_dir and source_file_name:
        if "path" in parsed_contents_df.columns:
            ensure_doc_nav_json(
                str(add_dir),
                chunks,
                source_file_name=str(source_file_name),
            )
        try:
            kb_dir_for_enrich = os.path.dirname(str(add_dir))
            summary_use_llm = JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_use_llm",
                False,
            )
            enrich_doc_nav_summaries(
                kb_dir_for_enrich,
                source_file=str(source_file_name),
                use_llm=summary_use_llm,
            )
            section_summaries = build_section_summary_lookup(str(add_dir))
        except Exception as exc:
            logger.warning(f"doc_nav enrichment failed (non-fatal): {exc}")
        document_top_summary = load_nav_top_summary(str(add_dir), str(source_file_name))

    if document_top_summary:
        for chunk in chunks:
            metadata = chunk.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                chunk["metadata"] = metadata
            metadata["document_top_summary"] = document_top_summary

    lifecycle_service.update_progress(
        job_id,
        progress=80,
        message="Generating ZIP package...",
    )
    processing_completed_at = datetime.now(timezone.utc)
    processing_timing_updates = {
        "processing_completed_at": processing_completed_at.isoformat(),
        "processing_duration_ms": max(
            0,
            int((processing_completed_at - processing_started_at).total_seconds() * 1000),
        ),
    }
    job_context.metadata_service.update_metadata(job_id, processing_timing_updates)
    job_context.job_metadata.update(processing_timing_updates)

    data_id = JobMetadataHelper.get_field(job_context.job_metadata, "data_id")
    zip_service = ZipResultService()
    zip_file_path, checksum, statistics, zip_size = zip_service.generate_zip_package(
        job_id=job_id,
        chunks=chunks,
        add_dir=str(add_dir) if add_dir else "",
        source_file_name=source_file_name,
        data_id=data_id,
        job_metadata=job_context.job_metadata,
        parsed_df=parsed_contents_df,
        temp_dir=task_workspace_dir,
    )
    del statistics

    checksum_value = (
        checksum.get("value", "")
        if isinstance(checksum, dict)
        else (checksum or "")
    )

    lifecycle_service.update_progress(
        job_id,
        progress=90,
        message="Uploading results to S3...",
    )
    result_bundle = get_result_storage().upload(
        job_id=job_id,
        result_dir=str(add_dir) if add_dir else "",
        zip_file_path=zip_file_path,
    )
    result_s3_key = result_bundle.zip_key
    stored_count = 0

    lifecycle_service.update_progress(job_id, progress=100, message="Task complete!")
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
