from __future__ import annotations

from app.services.document_ingestion.processing_run import DocumentProcessingRun

__all__ = ["parse_uploaded_file_job"]


def parse_uploaded_file_job(job_id: str, user_id: str | None) -> dict[str, object]:
    """Run worker-side Document Ingestion for an uploaded file Job."""
    return DocumentProcessingRun().execute(job_id, user_id)
