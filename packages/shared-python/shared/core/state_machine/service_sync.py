"""
Sync state machine service — core transition logic for Worker (gevent).

Mirror of ``AsyncStateMachineService`` using synchronous SQLAlchemy
``Session`` and ``SyncRedisService``.  Every public method signature matches
the async variant minus ``await``.
"""

import time
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import cast, literal, select, update
from sqlalchemy.orm import Session, load_only

from shared.core.state_machine.states import (
    JobStatus,
)
from shared.core.state_machine.transition_payloads import (
    build_failure_transition_metadata,
    build_progress_cache_payload,
    build_retry_transition,
    serialize_transition_metadata,
    utc_now_naive,
)
from shared.core.state_machine.transition_outcome import JobTransitionOutcome
from shared.core.state_machine.transition_runner import (
    MAX_TRANSITION_ATTEMPTS,
    TransitionJobSnapshot,
    build_cas_conflict_outcome,
    build_rollback_exception_outcome,
    build_transition_exception_outcome,
    get_cas_retry_delay_seconds,
    prepare_transition_attempt,
    should_retry_cas_conflict,
)
from shared.models.database.job import Job
from shared.models.database.job_state_audit_log import JobStateAuditLog
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class SyncStateMachineService:
    """Sync state machine service — used by the Worker (gevent + psycopg2)."""

    def __init__(self, redis_service=None):
        self.redis = redis_service or SyncRedisServiceFactory.get_service()

    # ── Public API ──────────────────────────────────────────────────────

    def transition(
        self,
        db: Session,
        job_id: str,
        to_state: str,
        transition_reason: str = "normal_transition",
        operator_id: Optional[str] = None,
        operator_type: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Execute an optimistic-lock state transition with up to 3 retries."""
        return self.transition_outcome(
            db,
            job_id,
            to_state,
            transition_reason,
            operator_id,
            operator_type,
            metadata,
        ).as_bool()

    def transition_outcome(
        self,
        db: Session,
        job_id: str,
        to_state: str,
        transition_reason: str = "normal_transition",
        operator_id: Optional[str] = None,
        operator_type: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> JobTransitionOutcome:
        """Execute a state transition and preserve the reason if it is rejected."""
        max_retries = MAX_TRANSITION_ATTEMPTS

        for attempt in range(max_retries):
            attempts = attempt + 1
            try:
                job = self._get_job_with_version(db, job_id)
                prepared = prepare_transition_attempt(
                    job_id=job_id,
                    to_state=to_state,
                    snapshot=(
                        TransitionJobSnapshot(status=job.status, version=job.version)
                        if job
                        else None
                    ),
                    attempts=attempts,
                )
                if prepared.outcome is not None:
                    if prepared.outcome.reason == "job_not_found":
                        logger.error(f"Job {job_id} does not exist")
                    elif prepared.outcome.reason == "invalid_transition":
                        logger.warning(
                            f"Job {job_id}: illegal transition "
                            f"{prepared.outcome.from_state} → {to_state}, rejected"
                        )
                    return prepared.outcome

                if not prepared.can_write:
                    logger.error(f"Job {job_id} does not exist")
                    return build_transition_exception_outcome(
                        job_id=job_id,
                        to_state=to_state,
                        attempts=attempts,
                        error="transition precondition did not provide state/version",
                    )

                old_state = prepared.from_state
                old_version = prepared.version
                assert old_state is not None
                assert old_version is not None

                self._record_audit_log(
                    db,
                    job_id,
                    old_state,
                    to_state,
                    transition_reason,
                    operator_id,
                    operator_type,
                    metadata,
                )

                success = self._cas_update_state(db, job_id, to_state, old_version)
                if success:
                    self._update_redis_cache(job_id, to_state, metadata)
                    db.flush()
                    logger.info(
                        f"Job {job_id} state transition: {old_state} → {to_state}"
                    )
                    return JobTransitionOutcome.transitioned(
                        job_id=job_id,
                        from_state=old_state,
                        to_state=to_state,
                        attempts=attempts,
                    )

                if should_retry_cas_conflict(attempt, max_attempts=max_retries):
                    logger.warning(
                        f"Job {job_id} CAS conflict, retry {attempt + 1}/{max_retries}"
                    )
                    import gevent

                    gevent.sleep(get_cas_retry_delay_seconds(attempt))
                    continue
                else:
                    logger.error(f"Job {job_id} CAS retries exhausted")
                    return build_cas_conflict_outcome(
                        job_id=job_id,
                        from_state=old_state,
                        to_state=to_state,
                        attempts=attempts,
                    )

            except Exception as e:
                logger.error(f"Job {job_id} transition failed: {e}")
                try:
                    if db.is_active:
                        db.rollback()
                except Exception as rollback_err:
                    logger.warning(f"Job {job_id} rollback failed: {rollback_err}")
                    return build_rollback_exception_outcome(
                        job_id=job_id,
                        to_state=to_state,
                        attempts=attempts,
                        error=rollback_err,
                    )
                return build_transition_exception_outcome(
                    job_id=job_id,
                    to_state=to_state,
                    attempts=attempts,
                    error=e,
                )

        return build_cas_conflict_outcome(
            job_id=job_id,
            to_state=to_state,
            attempts=max_retries,
            from_state=None,
        )

    def mark_failed(
        self,
        db: Session,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a job as failed with error information."""
        return self.mark_failed_outcome(
            db,
            job_id,
            error_message,
            error_code=error_code,
            error_details=error_details,
            operator_id=operator_id,
            metadata=metadata,
        ).as_bool()

    def mark_failed_outcome(
        self,
        db: Session,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> JobTransitionOutcome:
        """Mark a job as failed and expose why the transition was rejected."""
        try:
            normalized_details, transition_metadata = (
                build_failure_transition_metadata(
                    error_message=error_message,
                    error_code=error_code,
                    error_details=error_details,
                    metadata=metadata,
                )
            )
            self._update_job_error(
                db,
                job_id,
                error_message,
                error_code,
                normalized_details,
            )

            return self.transition_outcome(
                db,
                job_id,
                JobStatus.FAILED.value,
                "mark_failed",
                operator_id,
                "system",
                transition_metadata,
            )
        except Exception as e:
            logger.error(f"Failed to mark Job {job_id} as failed: {e}")
            return JobTransitionOutcome.rejected(
                job_id=job_id,
                to_state=JobStatus.FAILED.value,
                reason="transition_exception",
                attempts=1,
                error_message=str(e),
            )

    def mark_completed(
        self,
        db: Session,
        job_id: str,
        result_metadata: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> bool:
        """Mark a job as completed."""
        return self.mark_completed_outcome(
            db,
            job_id,
            result_metadata=result_metadata,
            operator_id=operator_id,
        ).as_bool()

    def mark_completed_outcome(
        self,
        db: Session,
        job_id: str,
        result_metadata: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> JobTransitionOutcome:
        """Mark a job as completed and expose why the transition was rejected."""
        try:
            return self.transition_outcome(
                db,
                job_id,
                JobStatus.DONE.value,
                "mark_completed",
                operator_id,
                "system",
                result_metadata,
            )
        except Exception as e:
            logger.error(f"Failed to mark Job {job_id} as completed: {e}")
            return JobTransitionOutcome.rejected(
                job_id=job_id,
                to_state=JobStatus.DONE.value,
                reason="transition_exception",
                attempts=1,
                error_message=str(e),
            )

    def handle_retry(
        self,
        db: Session,
        job_id: str,
        retry_metadata: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> bool:
        """Handle task retry — always goes through CAS-protected transition."""
        try:
            job = self._get_job(db, job_id)
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

            return self.transition(
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
                    db.rollback()
            except Exception as rollback_err:
                logger.warning(f"Job {job_id} rollback failed: {rollback_err}")
            return False

    # ── Private helpers ─────────────────────────────────────────────────

    def _get_job(self, db: Session, job_id: str) -> Optional[Job]:
        result = db.execute(select(Job).where(Job.job_id == job_id))
        return result.scalar_one_or_none()

    def _get_job_with_version(self, db: Session, job_id: str) -> Optional[Job]:
        result = db.execute(
            select(Job)
            .options(load_only(Job.job_id, Job.status, Job.version))
            .where(Job.job_id == job_id)
        )
        return result.scalar_one_or_none()

    def _cas_update_state(
        self,
        db: Session,
        job_id: str,
        to_state: str,
        old_version: int,
    ) -> bool:
        result = db.execute(
            update(Job)
            .where(Job.job_id == job_id, Job.version == old_version)
            .values(
                status=to_state,
                version=old_version + 1,
                updated_at=utc_now_naive(),
            )
        )
        return result.rowcount > 0

    def _record_audit_log(
        self,
        db: Session,
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

    def _update_job_error(
        self,
        db: Session,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            update_values: Dict[str, Any] = {
                "error_message": error_message,
                "error_code": error_code,
            }

            if error_details:
                import json as _json

                from sqlalchemy import func
                from sqlalchemy.dialects.postgresql import JSONB
                from sqlalchemy.dialects.postgresql import array as pg_array

                update_values["job_metadata"] = func.jsonb_set(
                    func.coalesce(Job.job_metadata.cast(JSONB), cast(literal("{}"), JSONB)),
                    pg_array(["error_details"]),
                    cast(literal(_json.dumps(error_details)), JSONB),
                )

            db.execute(update(Job).where(Job.job_id == job_id).values(**update_values))
        except Exception as e:
            logger.error(f"Failed to update Job {job_id} error info: {e}")

    def _update_redis_cache(
        self,
        job_id: str,
        status: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        try:
            status_key = redis_key_builder.task_status(job_id)
            self.redis.set(
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

            self.redis.hset(progress_key, mapping=progress_data)
            self.redis.expire(
                progress_key,
                redis_key_builder.get_key_ttl(RedisKeyType.TASK),
            )
        except Exception as e:
            logger.error(f"Redis cache update failed for Job {job_id}: {e}")
