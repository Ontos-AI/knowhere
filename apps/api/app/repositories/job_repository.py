"""Job repository."""

from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from loguru import logger
from sqlalchemy import and_, desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.core.state_machine.service import AsyncStateMachineService
from shared.models.database.job import Job
from shared.models.database.job_state_history import JobStateHistory


class JobRepository:
    """Repository for Job persistence operations."""

    def __init__(self):
        self.state_machine = AsyncStateMachineService()

    async def create_job(
        self,
        db: AsyncSession,
        user_id: str,
        job_type: str,
        source_type: str,
        job_id: Optional[str] = None,
        file_path: Optional[str] = None,
        webhook_url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        initial_state: Optional[str] = "pending",
        s3_key: Optional[str] = None,
    ) -> Optional[Job]:
        """Create a Job."""
        try:
            job_kwargs = dict(
                user_id=user_id,
                job_type=job_type,
                status=initial_state or "pending",
                source_type=source_type,
                file_path=file_path,
                webhook_url=webhook_url,
                webhook_enabled=bool(webhook_url),
                job_metadata=metadata,
                s3_key=s3_key,
            )
            if job_id:
                job_kwargs["job_id"] = job_id

            job = Job(**job_kwargs)

            db.add(job)
            await db.commit()
            # Skip db.refresh() — all fields are already set in-memory.
            # This avoids an extra SELECT round-trip after INSERT.

            logger.info(f"Job {job.job_id} created successfully")
            return job

        except IntegrityError as e:
            logger.error(f"Failed to create job: {e}")
            await db.rollback()
            raise
        except Exception as e:
            logger.error(f"Failed to create job: {e}")
            await db.rollback()
            return None

    async def get_job_by_id(self, db: AsyncSession, job_id: str) -> Optional[Job]:
        """Get a Job by ID, loading only job_result eagerly."""
        try:
            stmt = (
                select(Job)
                .options(selectinload(Job.job_result))
                .where(Job.job_id == job_id)
            )
            result = await db.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Failed to get job {job_id}: {e}")
            return None

    async def get_jobs_by_user(
        self,
        db: AsyncSession,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        job_type: Optional[str] = None,
        job_status: Optional[str] = None,
    ) -> Sequence[Job]:
        """Get jobs for a user."""
        try:
            stmt = (
                select(Job)
                .options(selectinload(Job.job_result))
                .where(Job.user_id == user_id)
                .order_by(desc(Job.created_at))
                .limit(limit)
                .offset(offset)
            )
            if created_after:
                stmt = stmt.where(Job.created_at >= created_after)
            if created_before:
                stmt = stmt.where(Job.created_at <= created_before)
            if job_type:
                stmt = stmt.where(Job.job_type == job_type)
            if job_status:
                stmt = stmt.where(Job.status == job_status)
            result = await db.execute(stmt)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Failed to get jobs for user {user_id}: {e}")
            return []

    async def count_jobs_by_user(
        self,
        db: AsyncSession,
        user_id: str,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        job_type: Optional[str] = None,
        job_status: Optional[str] = None,
    ) -> int:
        """Count jobs for a user."""
        try:
            from sqlalchemy import func

            stmt = select(func.count()).select_from(Job).where(Job.user_id == user_id)
            if created_after:
                stmt = stmt.where(Job.created_at >= created_after)
            if created_before:
                stmt = stmt.where(Job.created_at <= created_before)
            if job_type:
                stmt = stmt.where(Job.job_type == job_type)
            if job_status:
                stmt = stmt.where(Job.status == job_status)
            result = await db.execute(stmt)
            return result.scalar() or 0
        except Exception as e:
            logger.error(f"Failed to count jobs for user {user_id}: {e}")
            return 0

    async def update_job_state(
        self,
        db: AsyncSession,
        job_id: str,
        to_state: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update Job state through the state machine."""
        return await self.state_machine.transition(
            db, job_id, to_state, "repository_update", None, "system", metadata
        )

    async def update_job_s3_key(
        self, db: AsyncSession, job_id: str, s3_key: str
    ) -> bool:
        """Update the Job S3 key."""
        try:
            stmt = update(Job).where(Job.job_id == job_id).values(s3_key=s3_key)
            result = await db.execute(stmt)
            await db.commit()
            return result.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update S3 key for job {job_id}: {e}")
            await db.rollback()
            return False

    async def update_job_file_url(
        self, db: AsyncSession, job_id: str, file_url: str
    ) -> bool:
        """Update the Job file URL using direct UPDATE plus Redis refresh."""
        try:
            from sqlalchemy import func, literal
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.dialects.postgresql import array as pg_array

            from shared.services.redis import RedisServiceFactory
            from shared.services.redis.job_metadata_service import JobMetadataService

            # Direct UPDATE using jsonb_set to patch only the file_url key
            stmt = (
                update(Job)
                .where(Job.job_id == job_id)
                .values(
                    job_metadata=func.jsonb_set(
                        func.coalesce(
                            Job.job_metadata.cast(JSONB),
                            func.cast(literal("{}"), JSONB),
                        ),
                        pg_array(["file_url"]),
                        func.to_jsonb(file_url),
                    )
                )
            )
            result = await db.execute(stmt)
            await db.commit()

            if result.rowcount == 0:
                return False

            # Update Redis cache
            redis_service = RedisServiceFactory.get_service()
            metadata_service = JobMetadataService(redis_service)
            await metadata_service.update_metadata(job_id, {"file_url": file_url})
            return True

        except Exception as e:
            logger.error(f"Failed to update file URL for job {job_id}: {e}")
            await db.rollback()
            return False

    async def mark_job_failed(
        self, db: AsyncSession, job_id: str, error_message: str
    ) -> bool:
        """Mark a Job as failed."""
        return await self.state_machine.mark_failed(db, job_id, error_message)

    async def mark_job_completed(
        self,
        db: AsyncSession,
        job_id: str,
        result_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a Job as completed."""
        return await self.state_machine.mark_completed(db, job_id, result_metadata)

    async def get_job_state_history(
        self, db: AsyncSession, job_id: str
    ) -> Sequence[JobStateHistory]:
        """Get Job state history."""
        try:
            result = await db.execute(
                select(JobStateHistory)
                .where(JobStateHistory.job_id == job_id)
                .order_by(JobStateHistory.created_at)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Failed to get status history for job {job_id}: {e}")
            return []

    async def get_jobs_by_status(
        self, db: AsyncSession, status: str, limit: int = 100
    ) -> Sequence[Job]:
        """Get Jobs by status."""
        try:
            result = await db.execute(
                select(Job)
                .options(selectinload(Job.job_result))
                .where(Job.status == status)
                .order_by(Job.created_at)
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Failed to get jobs with status {status}: {e}")
            return []

    async def update_job_status(
        self,
        db: AsyncSession,
        job_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update Job status with a direct UPDATE and no ORM load."""
        try:
            values: Dict[str, Any] = {"status": status}
            if error_message:
                values["error_message"] = error_message

            stmt = update(Job).where(Job.job_id == job_id).values(**values)
            result = await db.execute(stmt)
            await db.commit()

            if result.rowcount > 0:
                logger.info(f"Job {job_id} status updated to {status}")
                return True
            return False
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to update status for job {job_id}: {e}")
            return False

    async def get_job_state_metadata(
        self, db: AsyncSession, job_id: str, state: str, metadata_key: str
    ) -> Optional[Any]:
        """Get specific metadata from Job state history."""
        try:
            result = await db.execute(
                select(JobStateHistory.transition_metadata)
                .where(
                    and_(
                        JobStateHistory.job_id == job_id,
                        JobStateHistory.to_state == state,
                    )
                )
                .order_by(desc(JobStateHistory.created_at))
                .limit(1)
            )
            metadata = result.scalar_one_or_none()

            if metadata:
                # If metadata is a string, try to parse it as JSON.
                if isinstance(metadata, str):
                    try:
                        import json

                        metadata = json.loads(metadata)
                    except json.JSONDecodeError as e:
                        logger.error(
                            f"Failed to parse JSON metadata for job {job_id} state {state}: {e}"
                        )
                        return None

                # If metadata is already a dict, return the requested key.
                if isinstance(metadata, dict):
                    return metadata.get(metadata_key)

            return None

        except Exception as e:
            logger.error(
                f"Failed to get metadata {metadata_key} for job {job_id} state {state}: {e}"
            )
            return None

    async def get_job_metadata(
        self, db: AsyncSession, job_id: str, redis_service: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get job_metadata, preferring Redis with a two-hour cache window.

        Args:
            db: Database session.
            job_id: Job ID.
            redis_service: Redis service.

        Returns:
            job_metadata dict or None.
        """
        # 1. Try Redis first.
        if redis_service:
            from shared.services.redis.job_metadata_service import JobMetadataService

            metadata_service = JobMetadataService(redis_service)
            metadata = await metadata_service.get_metadata(job_id)
            if metadata:
                return metadata

        # 2. Lightweight scalar query — fetch only the JSON column.
        try:
            result = await db.execute(
                select(Job.job_metadata).where(Job.job_id == job_id)
            )
            metadata = result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Failed to get metadata for job {job_id}: {e}")
            metadata = None

        if metadata:
            # Write back to Redis with a two-hour cache.
            if redis_service:
                from shared.services.redis.job_metadata_service import (
                    JobMetadataService,
                )

                metadata_service = JobMetadataService(redis_service)
                await metadata_service.save_metadata(job_id, metadata)
            return metadata

        return None
