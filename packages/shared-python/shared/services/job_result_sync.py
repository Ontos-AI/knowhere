from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.job_result import JobChunk, JobResult


class SyncJobResultWriter:
    """Persist terminal Job Result artifacts inside an existing transaction."""

    def upsert_job_result(
        self,
        db: Session,
        job_id: str,
        delivery_mode: str,
        *,
        inline_payload: dict[str, Any] | None = None,
        result_s3_key: str | None = None,
        result_size: int | None = None,
    ) -> JobResult:
        result = db.execute(select(JobResult).where(JobResult.job_id == job_id))
        existing = result.scalar_one_or_none()

        if existing:
            existing.delivery_mode = delivery_mode
            existing.inline_payload = inline_payload
            existing.result_s3_key = result_s3_key
            existing.result_size = result_size
            db.flush()
            return existing

        job_result = JobResult(
            job_id=job_id,
            delivery_mode=delivery_mode,
            document_metadata={},
            inline_payload=inline_payload,
            result_s3_key=result_s3_key,
            result_size=result_size,
        )
        db.add(job_result)
        db.flush()
        return job_result

    def replace_chunks(
        self,
        db: Session,
        job_result_id: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        db.execute(delete(JobChunk).where(JobChunk.job_result_id == job_result_id))

        if not chunks:
            db.flush()
            return

        chunk_models = []
        for index, chunk in enumerate(chunks):
            chunk_identifier = chunk.get("chunk_id") or str(uuid4())
            metadata = chunk.get("metadata")
            chunk_text = chunk.get("text") or chunk.get("content")
            chunk_path = (
                metadata.get("path")
                if isinstance(metadata, dict) and metadata.get("path")
                else chunk.get("path")
            )
            chunk_models.append(
                JobChunk(
                    job_result_id=job_result_id,
                    chunk_id=chunk_identifier,
                    chunk_type=chunk.get("type", "paragraph"),
                    text=str(chunk_text) if chunk_text is not None else None,
                    path=str(chunk_path) if chunk_path is not None else None,
                    chunk_metadata=metadata,
                    sort_order=chunk.get("order", index),
                )
            )
        db.add_all(chunk_models)
        db.flush()
