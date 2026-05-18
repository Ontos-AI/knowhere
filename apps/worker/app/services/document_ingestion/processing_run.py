from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
    load_nav_top_summary,
)
from app.services.document_ingestion.job_state_gate import mark_job_running
from app.services.document_ingestion.page_estimator import PageEstimator
from app.services.document_ingestion.parse_result_package import (
    GeneratedResultPackage,
    ParseArtifact,
    build_parse_result_package,
)
from app.services.document_ingestion.processing_billing import (
    charge_parse_job_pages,
    record_processing_start,
)
from app.services.document_ingestion.processing_context import (
    ParseJobContext,
    assert_source_file_within_size_limit,
    load_parse_job_context,
)
from app.services.document_ingestion.workspace import (
    cleanup_task_workspace,
    create_task_workspace,
    download_s3_file_to_temp,
)
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.jobs.lifecycle.service import get_sync_job_lifecycle_service
from shared.services.redis.distributed_lock import RedisJobLock
from shared.services.redis.redis_sync_service import (
    SyncRedisServiceFactory,
)
from shared.services.storage.result_storage import get_result_storage
from shared.services.storage.zip_result_service import ZipResultService


class DocumentProcessingRun:
    """Run worker-side Document Ingestion for an uploaded file Job."""

    def execute(self, job_id: str, user_id: str | None) -> dict[str, object]:
        logger.info(f"Parse started: job_id={job_id}, user_id={user_id}")
        lifecycle_service = get_sync_job_lifecycle_service()

        redis_service = SyncRedisServiceFactory.get_service()
        job_context = load_parse_job_context(job_id, user_id, redis_service)
        assert_source_file_within_size_limit(job_context.s3_key)

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
                result = _run_parse_job(
                    job_id=job_id,
                    job_context=job_context,
                    lifecycle_service=lifecycle_service,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    task_workspace_dir=task_workspace_dir,
                )
            finally:
                cleanup_task_workspace(task_workspace_dir)

        return result


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
    job_context: ParseJobContext,
    lifecycle_service: Any,
    input_dir: str,
    output_dir: str,
    task_workspace_dir: str,
) -> dict[str, object]:
    lifecycle_service.update_progress(job_id, progress=10, message="Parsing document...")

    filename = JobMetadataHelper.get_source_file_name(
        job_context.job_metadata,
    ) or os.path.basename(job_context.s3_key)
    file_ext = os.path.splitext(job_context.s3_key)[1].lower() if job_context.s3_key else ""
    local_temp_path = download_s3_file_to_temp(job_context.s3_key, file_ext, input_dir)
    logger.info(f"File downloaded: job_id={job_id}, local_path={local_temp_path}")

    from app.services.document_parser.support.internal_parse_name import (
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

    workload_estimate = PageEstimator.estimate_workload(local_temp_path)
    page_count = workload_estimate.page_count
    logger.info(
        "Workload estimation: "
        f"job_id={job_id}, page_count={page_count}, "
        f"method={workload_estimate.method}, "
        f"fallback_reason={workload_estimate.fallback_reason}"
    )

    processing_started_at = datetime.now(timezone.utc)
    billing_snapshot = charge_parse_job_pages(
        job_id=job_id,
        filename=filename,
        job_user_id=job_context.job_user_id,
        workload_estimate=workload_estimate,
    )
    record_processing_start(
        job_id=job_id,
        job_context=job_context,
        billing_snapshot=billing_snapshot,
        processing_started_at=processing_started_at,
        workload_estimate=workload_estimate,
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
        namespace = JobMetadataHelper.get_namespace(
            job_context.job_metadata,
            "default",
        )
        add_dir, parsed_contents_df = parse_service.checkerboard_inject_parse(
            file_full_path=local_temp_path,
            filename=filename,
            output_dir=output_dir,
            job_id=job_id,
            internal_output_filename=internal_parse_name,
            namespace=namespace or "default",
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

    lifecycle_service.update_progress(
        job_id,
        progress=30,
        message="Parse completed, preparing chunks...",
    )
    result_package = build_parse_result_package(
        job_id=job_id,
        filename=filename,
        add_dir=add_dir,
        parsed_contents_df=parsed_contents_df,
    )

    lifecycle_service.update_progress(
        job_id,
        progress=70,
        message="Chunks ready, generating zip...",
    )
    logger.info(
        f"Chunks prepared: job_id={job_id}, count={len(result_package.chunks)}"
    )

    return _finalize_parse_job_success(
        parse_artifact=result_package.artifact,
        chunks=result_package.chunks,
        job_context=job_context,
        job_id=job_id,
        lifecycle_service=lifecycle_service,
        processing_started_at=processing_started_at,
        task_workspace_dir=task_workspace_dir,
    )


def _finalize_parse_job_success(
    *,
    parse_artifact: ParseArtifact,
    chunks: list[dict[str, Any]],
    job_context: ParseJobContext,
    job_id: str,
    lifecycle_service: Any,
    processing_started_at: datetime,
    task_workspace_dir: str,
) -> dict[str, object]:
    source_file_name = JobMetadataHelper.get_source_file_name(
        job_context.job_metadata,
    ) or JobMetadataHelper.get_source_url(job_context.job_metadata)
    if not source_file_name:
        source_file_name = os.path.basename(job_context.s3_key)
    if isinstance(source_file_name, str) and "/" in source_file_name:
        source_file_name = os.path.basename(source_file_name)

    document_top_summary = ""
    section_summaries: dict[str, str] = {}
    add_dir = parse_artifact.add_dir
    parsed_contents_df = parse_artifact.dataframe
    if add_dir and source_file_name:
        if "path" in parsed_contents_df.columns:
            ensure_doc_nav_json(
                str(add_dir),
                chunks,
                source_file_name=str(source_file_name),
            )
        try:
            document_root_for_enrich = os.path.dirname(str(add_dir))
            summary_use_llm = JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_use_llm",
                False,
            )
            enrich_doc_nav_summaries(
                document_root_for_enrich,
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

    data_id = JobMetadataHelper.get_data_id(job_context.job_metadata)
    zip_service = ZipResultService()
    generated_package = GeneratedResultPackage.from_legacy_tuple(
        *zip_service.generate_zip_package(
            job_id=job_id,
            chunks=chunks,
            add_dir=str(add_dir) if add_dir else "",
            source_file_name=source_file_name,
            data_id=data_id,
            job_metadata=job_context.job_metadata,
            parsed_df=parsed_contents_df,
            temp_dir=task_workspace_dir,
        )
    )

    lifecycle_service.update_progress(
        job_id,
        progress=90,
        message="Uploading results to S3...",
    )
    result_bundle = get_result_storage().upload(
        job_id=job_id,
        result_dir=str(add_dir) if add_dir else "",
        zip_file_path=generated_package.zip_file_path,
    )
    result_s3_key = result_bundle.zip_key
    stored_count = 0

    finalization_response = lifecycle_service.finalize_job_success(
        job_id=job_id,
        chunks=chunks,
        result_s3_key=result_s3_key,
        checksum=generated_package.checksum_value,
        zip_size=generated_package.zip_size,
        stored_count=stored_count,
        delivery_mode="url",
        section_summaries=section_summaries,
    )
    if finalization_response.get("status") != "success":
        logger.error(
            f"Worker processing finalization failed: job_id={job_id}, "
            f"response={finalization_response}"
        )
        return dict(finalization_response)

    lifecycle_service.update_progress(job_id, progress=100, message="Task complete!")

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
