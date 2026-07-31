from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.services.connect_builder.summary_builder import (
    build_section_summary_lookup,
    enrich_doc_nav_summaries,
    ensure_doc_nav_json,
    load_nav_top_summary,
)
from app.services.document_ingestion.parse_result_package import (
    GeneratedResultPackage,
    ParseArtifact,
    ParseResultPackage,
    build_generated_result_package,
)
from app.services.document_ingestion.artifact_refs import collect_referenced_artifact_refs
from app.services.document_ingestion.processing_context import ParseJobContext
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.ai.token_tracking import get_current_token_tracker
from app.services.document_parser.support.stage_profiler import get_current_stage_tracker
from shared.services.storage.result_storage import ResultStorage, get_result_storage
from shared.services.storage.zip_result_service import ZipResultService

ResultStorageFactory = Callable[[], ResultStorage]


def finalize_parse_success(
    *,
    result_package: ParseResultPackage,
    job_context: ParseJobContext,
    job_id: str,
    lifecycle_service: Any,
    processing_started_at: datetime,
    task_workspace_dir: str,
    result_storage_factory: ResultStorageFactory = get_result_storage,
) -> dict[str, object]:
    """Package, upload, and publish a successful parser result."""
    source_file_name = _resolve_source_file_name(job_context)
    document_top_summary, section_summaries = _enrich_document_navigation(
        artifact=result_package.artifact,
        chunks=result_package.chunks,
        job_context=job_context,
        source_file_name=source_file_name,
    )
    _refresh_processing_stages(job_context)

    lifecycle_service.update_progress(
        job_id,
        progress=80,
        message="Generating ZIP package...",
    )
    _record_processing_completion(
        job_id=job_id,
        job_context=job_context,
        processing_started_at=processing_started_at,
    )
    _refresh_processing_stages(job_context)
    generated_package = _generate_result_package(
        result_package=result_package,
        job_context=job_context,
        job_id=job_id,
        source_file_name=source_file_name,
        task_workspace_dir=task_workspace_dir,
    )

    lifecycle_service.update_progress(
        job_id,
        progress=90,
        message="Uploading results to S3...",
    )
    result_s3_key = _upload_result_package(
        result_package=result_package,
        generated_package=generated_package,
        job_id=job_id,
        result_storage_factory=result_storage_factory,
    )
    stored_count = 0

    finalization_response = lifecycle_service.finalize_job_success(
        job_id=job_id,
        chunks=result_package.chunks,
        result_s3_key=result_s3_key,
        checksum=generated_package.checksum_value,
        zip_size=generated_package.zip_size,
        stored_count=stored_count,
        delivery_mode="url",
        section_summaries=section_summaries,
        document_top_summary=document_top_summary,
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
        "contents_count": result_package.artifact.contents_count,
        "stored_count": stored_count,
        "delivery_mode": "url",
        "result_s3_key": result_s3_key,
    }


def _resolve_source_file_name(job_context: ParseJobContext) -> str:
    source_file_name = JobMetadataHelper.get_source_file_name(
        job_context.job_metadata,
    ) or JobMetadataHelper.get_source_url(job_context.job_metadata)
    if not source_file_name:
        source_file_name = os.path.basename(job_context.s3_key)
    if isinstance(source_file_name, str) and "/" in source_file_name:
        source_file_name = os.path.basename(source_file_name)
    return str(source_file_name)


def _enrich_document_navigation(
    *,
    artifact: ParseArtifact,
    chunks: list[dict[str, Any]],
    job_context: ParseJobContext,
    source_file_name: str,
) -> tuple[str, dict[str, str]]:
    document_top_summary = ""
    section_summaries: dict[str, str] = {}
    enrich_results: dict[str, str] = {}
    add_dir = artifact.add_dir
    has_paths = False
    if artifact.chunks:
        has_paths = any(str(chunk.get("path") or "").strip() for chunk in artifact.chunks)
    elif artifact.dataframe is not None and "path" in artifact.dataframe.columns:
        has_paths = True
    if add_dir and source_file_name:
        if has_paths:
            ensure_doc_nav_json(
                str(add_dir),
                chunks,
                source_file_name=source_file_name,
            )
        try:
            document_root_for_enrich = os.path.dirname(str(add_dir))
            summary_use_llm = JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "summary_use_llm",
                False,
            )
            top_summary_use_llm = JobMetadataHelper.get_parsing_param(
                job_context.job_metadata,
                "top_summary_use_llm",
                True,
            )
            enrich_results = enrich_doc_nav_summaries(
                document_root_for_enrich,
                source_file=source_file_name,
                use_llm=summary_use_llm,
                top_summary_use_llm=top_summary_use_llm,
                chunks=chunks,
            )
            section_summaries = build_section_summary_lookup(str(add_dir))
        except Exception as exc:
            logger.warning(f"doc_nav enrichment failed (non-fatal): {exc}")
            enrich_results = {}
        document_top_summary = str(
            enrich_results.get(source_file_name) or ""
        ).strip()
        if not document_top_summary:
            document_top_summary = load_nav_top_summary(str(add_dir), source_file_name)
    return document_top_summary, section_summaries


def _refresh_processing_stages(job_context: ParseJobContext) -> None:
    token_usage = get_current_token_tracker()
    timing_ms = get_current_stage_tracker()
    if token_usage is None and timing_ms is None:
        return

    current_stages = job_context.job_metadata.get("stages")
    stages = dict(current_stages) if isinstance(current_stages, dict) else {}
    if token_usage is not None:
        stages["token_usage"] = dict(token_usage)
    if timing_ms is not None:
        stages["timing_ms"] = dict(timing_ms)
    job_context.job_metadata["stages"] = stages


def _record_processing_completion(
    *,
    job_id: str,
    job_context: ParseJobContext,
    processing_started_at: datetime,
) -> None:
    processing_completed_at = datetime.now(timezone.utc)
    processing_timing_updates = {
        "processing_completed_at": processing_completed_at.isoformat(),
        "processing_duration_ms": max(
            0,
            int((processing_completed_at - processing_started_at).total_seconds() * 1000),
        ),
    }
    _refresh_processing_stages(job_context)
    if "stages" in job_context.job_metadata:
        processing_timing_updates["stages"] = job_context.job_metadata["stages"]
    job_context.metadata_service.update_metadata(job_id, processing_timing_updates)
    job_context.job_metadata.update(processing_timing_updates)


def _generate_result_package(
    *,
    result_package: ParseResultPackage,
    job_context: ParseJobContext,
    job_id: str,
    source_file_name: str,
    task_workspace_dir: str,
) -> GeneratedResultPackage:
    data_id = JobMetadataHelper.get_data_id(job_context.job_metadata)
    zip_service = ZipResultService()
    return build_generated_result_package(
        *zip_service.generate_zip_package(
            job_id=job_id,
            chunks=result_package.chunks,
            add_dir=str(result_package.artifact.add_dir)
            if result_package.artifact.add_dir
            else "",
            source_file_name=source_file_name,
            data_id=data_id,
            job_metadata=job_context.job_metadata,
            temp_dir=task_workspace_dir,
        )
    )


def _upload_result_package(
    *,
    result_package: ParseResultPackage,
    generated_package: GeneratedResultPackage,
    job_id: str,
    result_storage_factory: ResultStorageFactory,
) -> str:
    artifact_refs = collect_referenced_artifact_refs(result_package.chunks)
    add_dir = str(result_package.artifact.add_dir) if result_package.artifact.add_dir else ""
    if add_dir and os.path.isfile(os.path.join(add_dir, "source.pdf")):
        artifact_refs.add("source.pdf")
    result_bundle = result_storage_factory().upload(
        job_id=job_id,
        result_dir=add_dir,
        zip_file_path=generated_package.zip_file_path,
        artifact_refs=artifact_refs,
    )
    return result_bundle.zip_key
