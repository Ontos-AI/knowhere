"""
Async state machine service — core transition logic for API service.

Provides optimistic-lock (CAS) state transitions, audit logging, and Redis
cache management.  All methods accept an ``AsyncSession``.
"""

import asyncio
import time
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import cast, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from shared.core.state_machine.states import (
    JobStatus,
    is_valid_transition,
)
from shared.core.state_machine.transition_payloads import (
    build_failure_transition_metadata,
    build_progress_cache_payload,
    build_retry_transition,
    serialize_transition_metadata,
    utc_now_naive,
)
from shared.models.database.job import Job
from shared.models.database.job_state_audit_log import JobStateAuditLog
from shared.services.redis import RedisServiceFactory
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class AsyncStateMachineService:
    """Async state machine service — used by the API (FastAPI + asyncpg)."""

    def __init__(self, redis_service=None):
        self.redis = redis_service or RedisServiceFactory.get_service()

    # ── Public API ──────────────────────────────────────────────────────

    async def transition(
        self,
        db: AsyncSession,
        job_id: str,
        to_state: str,
        transition_reason: str = "normal_transition",
        operator_id: Optional[str] = None,
        operator_type: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        auto_commit: bool = True,
    ) -> bool:
        """Execute an optimistic-lock state transition with up to 3 retries."""
        max_retries = 3

        for attempt in range(max_retries):
            try:
                job = await self._get_job_with_version(db, job_id)
                if not job:
                    logger.error(f"Job {job_id} does not exist")
                    return False

                if not is_valid_transition(job.status, to_state):
                    logger.warning(
                        f"Job {job_id}: illegal transition {job.status} → {to_state}, rejected"
                    )
                    return False

                old_state = job.status
                old_version = job.version

                # Record audit log (before CAS so the INSERT is within the same tx)
                await self._record_audit_log(
                    db,
                    job_id,
                    old_state,
                    to_state,
                    transition_reason,
                    operator_id,
                    operator_type,
                    metadata,
                )

                # CAS update
                success = await self._cas_update_state(
                    db,
                    job_id,
                    to_state,
                    old_version,
                )
                if success:
                    await self._update_redis_cache(job_id, to_state, metadata)

                    if auto_commit:
                        await db.commit()
                    else:
                        await db.flush()

                    logger.info(
                        f"Job {job_id} state transition: {old_state} → {to_state}"
                    )
                    return True

                # CAS miss — retry with backoff
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Job {job_id} CAS conflict, retry {attempt + 1}/{max_retries}"
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                else:
                    logger.error(f"Job {job_id} CAS retries exhausted")
                    return False

            except Exception as e:
                logger.error(f"Job {job_id} transition failed: {e}")
                try:
                    if db.is_active:
                        await db.rollback()
                except Exception as rollback_err:
                    logger.warning(f"Job {job_id} rollback failed: {rollback_err}")
                return False

        return False

    async def mark_failed(
        self,
        db: AsyncSession,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        auto_commit: bool = True,
    ) -> bool:
        """Mark a job as failed with error information."""
        try:
            normalized_details, transition_metadata = (
                build_failure_transition_metadata(
                    error_message=error_message,
                    error_code=error_code,
                    error_details=error_details,
                    metadata=metadata,
                )
            )
            await self._update_job_error(
                db,
                job_id,
                error_message,
                error_code,
                normalized_details,
            )

            return await self.transition(
                db,
                job_id,
                JobStatus.FAILED.value,
                "mark_failed",
                operator_id,
                "system",
                transition_metadata,
                auto_commit=auto_commit,
            )
        except Exception as e:
            logger.error(f"Failed to mark Job {job_id} as failed: {e}")
            return False

    async def mark_completed(
        self,
        db: AsyncSession,
        job_id: str,
        result_metadata: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
        auto_commit: bool = True,
    ) -> bool:
        """Mark a job as completed."""
        try:
            return await self.transition(
                db,
                job_id,
                JobStatus.DONE.value,
                "mark_completed",
                operator_id,
                "system",
                result_metadata,
                auto_commit=auto_commit,
            )
        except Exception as e:
            logger.error(f"Failed to mark Job {job_id} as completed: {e}")
            return False

    async def handle_retry(
        self,
        db: AsyncSession,
        job_id: str,
        retry_metadata: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> bool:
        """Handle task retry — always goes through CAS-protected transition."""
        try:
            job = await self._get_job(db, job_id)
            if not job:
                logger.error(f"Job {job_id} does not exist")
                return False

            current_state = job.status
            if not current_state:
                logger.error(f"Job {job_id} has no status")
                return False

            retry_target, retry_metadata = build_retry_transition(
                current_state=current_state,
                retry_metadata=retry_metadata,
            )

            # Always use full transition() for CAS protection — even same-state
            return await self.transition(
                db,
                job_id,
                retry_target,
                "retry_transition",
                operator_id,
                "retry",
                retry_metadata,
            )
        except Exception as e:
            logger.error(f"Job {job_id} retry failed: {e}")
            try:
                if db.is_active:
                    await db.rollback()
            except Exception as rollback_err:
                logger.warning(f"Job {job_id} rollback failed: {rollback_err}")
            return False

    async def get_current_state(
        self,
        db: AsyncSession,
        job_id: str,
    ) -> Optional[str]:
        """Get current job state (Redis first, then DB fallback)."""
        try:
            redis_key = redis_key_builder.task_status(job_id)
            cached = await self.redis.get(redis_key)
            if cached:
                return cached
            job = await self._get_job(db, job_id)
            return job.status if job else None
        except Exception as e:
            logger.error(f"Failed to get Job {job_id} state: {e}")
            return None

    # ── Private helpers ─────────────────────────────────────────────────

    async def _get_job(
        self,
        db: AsyncSession,
        job_id: str,
    ) -> Optional[Job]:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        return result.scalar_one_or_none()

    async def _get_job_with_version(
        self,
        db: AsyncSession,
        job_id: str,
    ) -> Optional[Job]:
        result = await db.execute(
            select(Job)
            .options(load_only(Job.job_id, Job.status, Job.version))
            .where(Job.job_id == job_id)
        )
        return result.scalar_one_or_none()

    async def _cas_update_state(
        self,
        db: AsyncSession,
        job_id: str,
        to_state: str,
        old_version: int,
    ) -> bool:
        """Atomic compare-and-swap on (job_id, version)."""
        result = await db.execute(
            update(Job)
            .where(Job.job_id == job_id, Job.version == old_version)
            .values(
                status=to_state,
                version=old_version + 1,
                updated_at=utc_now_naive(),
            )
        )
        return result.rowcount > 0

    async def _record_audit_log(
        self,
        db: AsyncSession,
        job_id: str,
        from_state: str,
        to_state: str,
        transition_reason: str,
        operator_id: Optional[str],
        operator_type: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        serialized = serialize_transition_metadata(metadata)

        db.add(
            JobStateAuditLog(
                job_id=job_id,
                from_state=from_state,
                to_state=to_state,
                transition_reason=transition_reason,
                operator_id=operator_id,
                operator_type=operator_type,
                transition_metadata=serialized,
            )
        )

    async def _update_job_error(
        self,
        db: AsyncSession,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update error fields on the Job row (single UPDATE, no ORM load)."""
        try:
            from sqlalchemy import func
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.dialects.postgresql import array as pg_array

            update_values: Dict[str, Any] = {
                "error_message": error_message,
                "error_code": error_code,
            }

            if error_details:
                import json as _json

                update_values["job_metadata"] = func.jsonb_set(
                    func.coalesce(Job.job_metadata.cast(JSONB), cast(literal("{}"), JSONB)),
                    pg_array(["error_details"]),
                    cast(literal(_json.dumps(error_details)), JSONB),
                )

                from shared.services.redis.job_metadata_service import (
                    JobMetadataService,
                )

                metadata_svc = JobMetadataService(self.redis)
                await metadata_svc.update_metadata(
                    job_id,
                    {"error_details": error_details},
                )

            await db.execute(
                update(Job).where(Job.job_id == job_id).values(**update_values)
            )
        except Exception as e:
            logger.error(f"Failed to update Job {job_id} error info: {e}")
            try:
                await db.rollback()
            except Exception as rollback_err:
                logger.warning(
                    f"Rollback failed after error-update failure for Job {job_id}: {rollback_err}"
                )

    async def _update_redis_cache(
        self,
        job_id: str,
        status: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        try:
            status_key = redis_key_builder.task_status(job_id)
            await self.redis.set(
                status_key,
                status,
                ttl=redis_key_builder.get_key_ttl(RedisKeyType.TASK),
            )

            progress_key = redis_key_builder.task_progress(job_id)
            progress_data: Dict[str, Any] = build_progress_cache_payload(
                status=status,
                metadata=None,
                timestamp=int(time.time()),
            )
            if metadata:
                try:
                    progress_data.update(
                        build_progress_cache_payload(
                            status=status,
                            metadata=metadata,
                            timestamp=int(time.time()),
                        )
                    )
                except Exception as e:
                    logger.warning(f"Metadata serialization skipped: {e}")

            await self.redis.hset(progress_key, mapping=progress_data)
            await self.redis.expire(
                progress_key,
                redis_key_builder.get_key_ttl(RedisKeyType.TASK),
            )
        except Exception as e:
            logger.error(f"Redis cache update failed for Job {job_id}: {e}")
