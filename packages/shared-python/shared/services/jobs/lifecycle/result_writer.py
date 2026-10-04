from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from shared.models.database.job import Job
from shared.models.database.job_result import JobChunk, JobResult
from shared.utils.json_utils import remove_nul_characters


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

        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        metadata = job.job_metadata or {} if job is not None else {}
        demoMetadata = {key: metadata[key] for key in ("source_size_bytes", "demo_asset_manifest", "result_raw_prefix", "parse_track") if key in metadata} if metadata.get("corpus_target") == "DEMO" else {}
        job_result = JobResult(
            job_id=job_id,
            delivery_mode=delivery_mode,
            document_metadata=demoMetadata,
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

        chunk_rows: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            safe_chunk = cast(dict[str, Any], remove_nul_characters(chunk))
            chunk_identifier = safe_chunk.get("chunk_id") or str(uuid4())
            metadata = safe_chunk.get("metadata")
            chunk_text = safe_chunk.get("text") or safe_chunk.get("content")
            chunk_path = (
                metadata.get("path")
                if isinstance(metadata, dict) and metadata.get("path")
                else safe_chunk.get("path")
            )
            chunk_rows.append(
                {
                    "id": str(uuid4()),
                    "job_result_id": job_result_id,
                    "chunk_id": str(chunk_identifier),
                    "chunk_type": str(safe_chunk.get("type", "paragraph")),
                    "text": str(chunk_text) if chunk_text is not None else None,
                    "path": str(chunk_path) if chunk_path is not None else None,
                    "chunk_metadata": metadata,
                    "sort_order": safe_chunk.get("order", index),
                }
            )
        db.execute(insert(JobChunk), chunk_rows)
        db.flush()
