"""
Sync Job Lifecycle Service for Celery worker (gevent pool).

Encapsulates the complete job success/failure finalization that previously
used an API-side broker consumer. The worker now writes directly to the
database in a single atomic transaction,
using the same transactional outbox pattern for webhook events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Dict, List, Optional, TypeVar

from loguru import logger
from sqlalchemy.orm import Session

from shared.core.database_sync import get_sync_db_context
from shared.services.job_failure_sync import SyncJobFailureFinalizer
from shared.services.job_post_commit_effects_sync import (
    PostCommitEffectPlan,
    SyncJobPostCommitEffectRunner,
)
from shared.services.job_success_sync import SyncJobSuccessFinalizer
from shared.services.redis.redis_sync_service import (
    SyncRedisServiceFactory,
)
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class SyncJobLifecycleService:
    """Manages job lifecycle transitions in the worker process (sync/gevent).

    Implements the direct worker → DB write path for job completion and failure.
    """

    def __init__(self) -> None:
        self._success_finalizer = SyncJobSuccessFinalizer()
        self._failure_finalizer = SyncJobFailureFinalizer()
        self._post_commit_effect_runner = SyncJobPostCommitEffectRunner()

    # ── Public API ──────────────────────────────────────────────────────

    def finalize_job_success(
        self,
        job_id: str,
        result_s3_key: str,
        checksum: str,
        zip_size: int,
        chunks: Optional[List[Dict[str, Any]]] = None,
        stored_count: int = 0,
        delivery_mode: str = "url",
        section_summaries: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Finalize a successful job in a single atomic transaction.

        Steps (all within one DB transaction):
            1. Upsert JobResult + replace full result chunks
            2. Publish document state from full result chunks
            3. Mark job as DONE via state machine (CAS)
            4. Create WebhookEvent if webhook_enabled
            5. COMMIT
            6. Post-commit: enqueue webhook
        """
        logger.info(f"Finalizing job success: job_id={job_id}")

        return _run_lifecycle_transaction(
            job_id=job_id,
            label="success",
            finalize=lambda db: self._success_finalizer.finalize(
                db,
                job_id=job_id,
                result_s3_key=result_s3_key,
                checksum=checksum,
                zip_size=zip_size,
                chunks=chunks or [],
                stored_count=stored_count,
                delivery_mode=delivery_mode,
                section_summaries=section_summaries,
            ),
            should_commit=lambda finalization: finalization.response.get("status")
            == "success",
            build_response=lambda finalization: finalization.response,
            build_effect_plan=lambda finalization: finalization.post_commit_effects,
            run_after_commit_effects=self._post_commit_effect_runner.run,
        )

    def finalize_job_failure(
        self,
        job_id: str,
        error_message: str,
        error_code: str = "UNKNOWN",
        error_details: Optional[Dict[str, Any]] = None,
        should_refund: bool = False,
    ) -> bool:
        """Finalize a failed job in a single atomic transaction.

        Steps (all within one DB transaction):
            1. Mark job as FAILED via state machine (CAS + error fields)
            2. Refund credits if needed
            3. Create WebhookEvent if webhook_enabled
            4. COMMIT
            5. Post-commit: enqueue webhook
        """
        logger.info(f"Finalizing job failure: job_id={job_id}")

        return _run_lifecycle_transaction(
            job_id=job_id,
            label="failure",
            finalize=lambda db: self._failure_finalizer.finalize(
                db,
                job_id=job_id,
                error_message=error_message,
                error_code=error_code,
                error_details=error_details,
                should_refund=should_refund,
            ),
            should_commit=lambda finalization: finalization.succeeded,
            build_response=lambda finalization: finalization.succeeded,
            build_effect_plan=lambda finalization: finalization.post_commit_effects,
            run_after_commit_effects=self._post_commit_effect_runner.run,
        )

    def update_progress(
        self,
        job_id: str,
        progress: int,
        message: str = "",
    ) -> bool:
        """Write job progress directly to Redis (replaces publish_progress_update).

        Best-effort — failures are logged but do not raise.
        """
        try:
            redis_service = SyncRedisServiceFactory.get_service()
            task_ttl = redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            progress_key = redis_service._build_key(
                redis_key_builder.task_progress(job_id)
            )

            pipe = redis_service.pipeline()
            pipe.hset(
                progress_key,
                mapping={
                    "progress": str(progress),
                    "message": message,
                    "timestamp": str(int(time.time())),
                },
            )
            pipe.expire(progress_key, task_ttl)
            pipe.execute()
            return True
        except Exception as exc:
            logger.warning(f"Failed to update progress for job {job_id}: {exc}")
            return False

# Module-level singleton
_lifecycle_service: Optional[SyncJobLifecycleService] = None


def get_sync_job_lifecycle_service() -> SyncJobLifecycleService:
    """Get the singleton sync job lifecycle service."""
    global _lifecycle_service
    if _lifecycle_service is None:
        _lifecycle_service = SyncJobLifecycleService()
    return _lifecycle_service


_FinalizationT = TypeVar("_FinalizationT")
_ResponseT = TypeVar("_ResponseT")


def _run_lifecycle_transaction(
    *,
    job_id: str,
    label: str,
    finalize: Callable[[Session], _FinalizationT],
    should_commit: Callable[[_FinalizationT], bool],
    build_response: Callable[[_FinalizationT], _ResponseT],
    build_effect_plan: Callable[[_FinalizationT], PostCommitEffectPlan],
    run_after_commit_effects: Callable[[PostCommitEffectPlan], None],
) -> _ResponseT:
    with get_sync_db_context() as db:
        try:
            finalization = finalize(db)
            if not should_commit(finalization):
                db.rollback()
                return build_response(finalization)

            db.commit()
            logger.info(f"Job {job_id} {label} transaction committed")

            run_after_commit_effects(build_effect_plan(finalization))
            return build_response(finalization)

        except Exception as exc:
            logger.error(f"Failed to finalize job {label} {job_id}: {exc}")
            db.rollback()
            raise
