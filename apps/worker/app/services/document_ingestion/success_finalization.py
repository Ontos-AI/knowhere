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
from app.services.document_ingestion.processing_context import ParseJobContext
from loguru import logger

from shared.models.schemas.job_metadata import JobMetadataHelper
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
    _attach_document_top_summary(result_package.chunks, document_top_summary)

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
    add_dir = artifact.add_dir
    parsed_contents_df = artifact.dataframe
    if add_dir and source_file_name:
        if "path" in parsed_contents_df.columns:
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
            enrich_doc_nav_summaries(
                document_root_for_enrich,
                source_file=source_file_name,
                use_llm=summary_use_llm,
            )
            section_summaries = build_section_summary_lookup(str(add_dir))
        except Exception as exc:
            logger.warning(f"doc_nav enrichment failed (non-fatal): {exc}")
        document_top_summary = load_nav_top_summary(str(add_dir), source_file_name)
    return document_top_summary, section_summaries


def _attach_document_top_summary(
    chunks: list[dict[str, Any]],
    document_top_summary: str,
) -> None:
    if not document_top_summary:
        return

    for chunk in chunks:
        metadata = chunk.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            chunk["metadata"] = metadata
        metadata["document_top_summary"] = document_top_summary


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
            parsed_df=result_package.artifact.dataframe,
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
    result_bundle = result_storage_factory().upload(
        job_id=job_id,
        result_dir=str(result_package.artifact.add_dir)
        if result_package.artifact.add_dir
        else "",
        zip_file_path=generated_package.zip_file_path,
    )
    return result_bundle.zip_key
