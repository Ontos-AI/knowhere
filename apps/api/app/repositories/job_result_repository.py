"""Job-result repository."""

from typing import Any, Dict, List, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.database.job_result import JobChunk, JobResult


class JobResultRepository:
    """Persistence access for job results."""

    async def get_by_job_id(
        self, db: AsyncSession, job_id: str, with_chunks: bool = False
    ) -> Optional[JobResult]:
        """Get a result by job ID."""
        stmt = select(JobResult).where(JobResult.job_id == job_id)
        if with_chunks:
            stmt = stmt.options(selectinload(JobResult.chunks))
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_job_result(
        self,
        db: AsyncSession,
        job_id: str,
        delivery_mode: str,
        document_metadata: Optional[Dict[str, Any]] = None,
        *,
        inline_payload: Optional[Dict[str, Any]] = None,
        result_s3_key: Optional[str] = None,
        result_size: Optional[int] = None,
    ) -> JobResult:
        """Create or update a job result."""
        existing = await self.get_by_job_id(db, job_id)
        if existing:
            existing.delivery_mode = delivery_mode
            existing.document_metadata = document_metadata or {}
            existing.inline_payload = inline_payload
            existing.result_s3_key = result_s3_key
            existing.result_size = result_size
            await db.flush()
            await db.refresh(existing)
            logger.info(
                f"Updated JobResult successfully: job_id={job_id}, mode={delivery_mode}"
            )
            return existing

        job_result = JobResult(
            job_id=job_id,
            delivery_mode=delivery_mode,
            document_metadata=document_metadata or {},
            inline_payload=inline_payload,
            result_s3_key=result_s3_key,
            result_size=result_size,
        )
        db.add(job_result)
        await db.flush()
        await db.refresh(job_result)
        logger.info(
            f"Created JobResult successfully: job_id={job_id}, mode={delivery_mode}"
        )
        return job_result

    async def replace_chunks(
        self, db: AsyncSession, job_result_id: str, chunks: List[Dict[str, Any]]
    ) -> None:
        """Replace the chunk list for the specified JobResult."""
        await db.execute(
            delete(JobChunk).where(JobChunk.job_result_id == job_result_id)
        )

        if chunks:
            chunk_models = []
            for index, chunk in enumerate(chunks):
                chunk_identifier = chunk.get("chunk_id") or str(uuid4())
                chunk_models.append(
                    JobChunk(
                        job_result_id=job_result_id,
                        chunk_id=chunk_identifier,
                        chunk_type=chunk.get("type", "paragraph"),
                        text=chunk.get("text"),
                        path=chunk.get("metadata", {}).get("path"),
                        chunk_metadata=chunk.get("metadata"),
                        sort_order=chunk.get("order", index),
                    )
                )
            db.add_all(chunk_models)

        await db.flush()

    async def delete_by_job_id(self, db: AsyncSession, job_id: str) -> None:
        """Delete a job result and its chunks."""
        result = await self.get_by_job_id(db, job_id)
        if not result:
            return
        if result.demo_document_id is not None:
            from shared.core.exceptions.domain_exceptions import ConflictException
            raise ConflictException(user_message="Shared demo revision results and their assets must be retained.", resource="Demo revision", resource_id=result.id)
        await db.delete(result)
        await db.flush()
