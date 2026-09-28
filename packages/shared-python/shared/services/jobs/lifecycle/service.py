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
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, TypeVar
from uuid import uuid4

from loguru import logger
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from shared.core.database_sync import get_sync_db_context
from shared.core.config import settings
from shared.services.jobs.lifecycle.failure_finalizer import SyncJobFailureFinalizer
from shared.services.jobs.lifecycle.post_commit_effects import (
    PostCommitEffectPlan,
    SyncJobPostCommitEffectRunner,
)
from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.services.jobs.lifecycle.publication_trace_sql import (
    install_publication_trace_sql_instrumentation,
    publication_trace_connection,
)
from shared.services.redis.redis_sync_service import (
    SyncRedisServiceFactory,
)
from shared.services.redis.key_builder import RedisKeyType, redis_key_builder


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
        document_top_summary: Optional[str] = None,
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
            trace_publication=True,
            finalize=lambda db, trace: self._finalize_success_with_trace(
                db=db,
                trace=trace,
                job_id=job_id,
                result_s3_key=result_s3_key,
                checksum=checksum,
                zip_size=zip_size,
                chunks=chunks or [],
                stored_count=stored_count,
                delivery_mode=delivery_mode,
                section_summaries=section_summaries,
                document_top_summary=document_top_summary,
            ),
            should_commit=lambda finalization: finalization.response.should_commit(),
            build_response=lambda finalization: finalization.response.to_dict(),
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
            finalize=lambda db, _trace: self._failure_finalizer.finalize(
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

    def _finalize_success_with_trace(
        self,
        *,
        db: Session,
        trace: PublicationTrace | None,
        job_id: str,
        result_s3_key: str,
        checksum: str,
        zip_size: int,
        chunks: list[dict[str, Any]],
        stored_count: int,
        delivery_mode: str,
        section_summaries: dict[str, str] | None,
        document_top_summary: str | None,
    ) -> Any:
        if trace is not None:
            counter_names = {
                "text": "input_text_chunks",
                "image": "input_image_chunks",
                "table": "input_table_chunks",
                "page": "input_page_chunks",
            }
            for chunk_type, counter_name in counter_names.items():
                trace.record_count(
                    counter_name,
                    sum(
                        1
                        for chunk in chunks
                        if str(chunk.get("type") or chunk.get("chunk_type") or "text")
                        == chunk_type
                    ),
                )
        return self._success_finalizer.finalize(
            db,
            job_id=job_id,
            result_s3_key=result_s3_key,
            checksum=checksum,
            zip_size=zip_size,
            chunks=chunks,
            stored_count=stored_count,
            delivery_mode=delivery_mode,
            section_summaries=section_summaries,
            document_top_summary=document_top_summary,
            trace=trace,
        )

    def update_progress(
        self,
        job_id: str,
        progress: int,
        message: str = "",
        redis_service: Any | None = None,
    ) -> bool:
        """Write job progress directly to Redis (replaces publish_progress_update).

        Best-effort — failures are logged but do not raise.
        """
        try:
            active_redis_service = (
                redis_service
                if redis_service is not None
                else SyncRedisServiceFactory.get_service()
            )
            task_ttl = redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            progress_key = redis_key_builder.task_progress(job_id)
            active_redis_service.hset(
                progress_key,
                mapping={
                    "progress": str(progress),
                    "message": message,
                    "timestamp": str(int(time.time())),
                },
            )
            active_redis_service.expire(progress_key, task_ttl)
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
    finalize: Callable[[Session, PublicationTrace | None], _FinalizationT],
    should_commit: Callable[[_FinalizationT], bool],
    build_response: Callable[[_FinalizationT], _ResponseT],
    build_effect_plan: Callable[[_FinalizationT], PostCommitEffectPlan],
    run_after_commit_effects: Callable[[PostCommitEffectPlan], None],
    trace_publication: bool = False,
) -> _ResponseT:
    trace = (
        PublicationTrace.start(
            attempt_ref=f"publication_{uuid4().hex}",
            owner="sync",
            job_id=job_id,
        )
        if trace_publication and settings.KNOWHERE_PUBLICATION_TRACE_ENABLED
        else None
    )
    with get_sync_db_context() as db:
        try:
            connection = None
            if trace is not None:
                bind = db.get_bind()
                install_publication_trace_sql_instrumentation(
                    bind.engine if isinstance(bind, Connection) else bind
                )
                with trace.stage("pool_checkout_wait"):
                    connection = db.connection()
            connection_scope = (
                publication_trace_connection(connection, trace)
                if connection is not None and trace is not None
                else nullcontext()
            )
            with connection_scope:
                finalization = finalize(db, trace)
                if not should_commit(finalization):
                    if trace is not None:
                        try:
                            with trace.stage("rollback"):
                                db.rollback()
                        finally:
                            trace.finish(outcome="rollback")
                    else:
                        db.rollback()
                    response = build_response(finalization)
                    return response

                response = build_response(finalization)
                effect_plan = build_effect_plan(finalization)
                if trace is not None:
                    with trace.stage("commit"):
                        db.commit()
                else:
                    db.commit()
            if trace is not None:
                trace.finish(outcome="success")
            logger.info(f"Job {job_id} {label} transaction committed")

            run_after_commit_effects(effect_plan)
            return response

        except Exception as exc:
            logger.error(f"Failed to finalize job {label} {job_id}: {exc}")
            if trace is None or not trace.is_finished:
                if trace is not None:
                    try:
                        with trace.stage("rollback"):
                            db.rollback()
                    finally:
                        trace.finish(outcome="rollback")
                else:
                    db.rollback()
            raise
