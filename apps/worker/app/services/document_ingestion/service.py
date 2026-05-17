from __future__ import annotations

import app.services.document_ingestion.processing_run as processing_run
from app.services.document_ingestion.processing_run import DocumentProcessingRun
from app.services.document_ingestion.processing_run import PageEstimator
from app.services.document_ingestion.processing_run import cleanup_task_workspace
from app.services.document_ingestion.processing_run import download_s3_file_to_temp
from app.services.document_ingestion.processing_run import get_result_storage
from app.services.document_ingestion.processing_run import settings

__all__ = [
    "PageEstimator",
    "cleanup_task_workspace",
    "download_s3_file_to_temp",
    "get_result_storage",
    "parse_uploaded_file_job",
    "settings",
]


def parse_uploaded_file_job(job_id: str, user_id: str | None) -> dict[str, object]:
    """Run worker-side Document Ingestion for an uploaded file Job."""
    _sync_legacy_adapter_overrides()
    return DocumentProcessingRun().execute(job_id, user_id)


def _sync_legacy_adapter_overrides() -> None:
    processing_run.download_s3_file_to_temp = download_s3_file_to_temp
    processing_run.get_result_storage = get_result_storage
    processing_run.cleanup_task_workspace = cleanup_task_workspace
