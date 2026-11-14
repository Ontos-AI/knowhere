"""
Stale Job Sweeper — periodic Celery Beat task.

Settles overdue guarded uploads and expires other stale active Jobs.
"""

from datetime import datetime, timedelta

from app.services.document_ingestion.guarded_upload_sweeper import (
    guarded_waiting_upload,
    sweep_guarded_uploads,
)
from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy import select as sa_select

from shared.core.celery_app import get_celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context, get_sync_session_factory
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.core.state_machine.states import (
    TERMINAL_STATES,
    JobStatus,
)
from shared.models.database.job import Job
from shared.services.jobs.upload_parse_dispatch import within_upload_parse_dispatch_window
from shared.services.redis.periodic_task_lock import periodic_task_lock
from shared.services.retrieval.lexical.settlement import IndexingSettlement
from shared.utils.utc_now import utc_now_naive

celery_app = get_celery_app()

JOB_EXPIRED_ERROR_MESSAGE = (
    "Job timed out: execution or upload exceeded maximum allowed duration"
)
JOB_EXPIRED_ERROR_CODE = "JOB_EXPIRED"

# Matches the beat_schedule period in celery_app.py
_TASK_PERIOD_SECONDS = 1800


@celery_app.task(name="app.core.tasks.stale_job_sweeper.expire_stale_jobs")
def expire_stale_jobs() -> dict:
    """Settle overdue uploads and expire stale parser Jobs.

    Runs every 30 minutes via Celery Beat. Uses ``SyncStateMachineService``
    for CAS-protected state transitions and proper audit logging.

    A ``periodic_task_lock`` guards against duplicate execution when multiple
    Beat processes fire this task in the same scheduling window.
    """
    with periodic_task_lock(
        "app.core.tasks.stale_job_sweeper.expire_stale_jobs",
        period_seconds=_TASK_PERIOD_SECONDS,
    ) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "duplicate Beat firing"}

        # Parsed revisions have their own short execution/admission deadlines.
        # This branch never restarts indexing and is independent of parser age.
        indexing: dict[str, int] = IndexingSettlement.sweep(get_sync_session_factory())

        waiting_max_age: int = settings.JOB_WAITING_EXPIRE_SECONDS
        processing_max_age: int = settings.JOB_PROCESSING_EXPIRE_SECONDS

        now = utc_now_naive()
        legacy_expiration_enabled = waiting_max_age > 0 and processing_max_age > 0
        try:
            expired_count, skipped_count = (
                _expire_other_stale_jobs(now, waiting_max_age, processing_max_age)
                if legacy_expiration_enabled
                else (0, 0)
            )
            guarded_expired, guarded_skipped = sweep_guarded_uploads(now)
            expired_count += guarded_expired
            skipped_count += guarded_skipped

            if expired_count > 0:
                logger.info(
                    f"Stale job sweeper completed: expired={expired_count}, "
                    f"skipped={skipped_count}"
                )
            return {
                "status": "success",
                "expired": expired_count,
                "skipped": skipped_count,
                "indexing": indexing,
                **({} if legacy_expiration_enabled else {"reason": "Parser expiration config <= 0"}),
            }

        except Exception as e:
            logger.error(f"Stale job sweeper failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


def _expire_other_stale_jobs(
    now: datetime, waiting_max_age: int, processing_max_age: int
) -> tuple[int, int]:
    long_cutoff = now - timedelta(seconds=processing_max_age)
    processing_states = (JobStatus.RUNNING.value, JobStatus.CONVERTING.value)
    state_machine = SyncStateMachineService()
    expired_count = 0
    skipped_count = 0
    with get_sync_db_context() as db:
        expired_jobs = db.scalars(
            sa_select(Job)
            .where(
                Job.status.notin_(TERMINAL_STATES),
                Job.parsing_status != "completed",
                ~guarded_waiting_upload,
                or_(
                    and_(Job.status.in_(processing_states), Job.updated_at < long_cutoff),
                    and_(
                        Job.status.notin_(processing_states),
                        ~within_upload_parse_dispatch_window(now=now, waiting_max_age=waiting_max_age),
                    ),
                ),
            )
            .order_by(Job.updated_at)
            .limit(200)
            .with_for_update(skip_locked=True)
        ).all()

        for job in expired_jobs:
            outcome = state_machine.mark_failed_outcome(
                db,
                job.job_id,
                error_message=JOB_EXPIRED_ERROR_MESSAGE,
                error_code=JOB_EXPIRED_ERROR_CODE,
                metadata={"sweeper": True, "stale_status": job.status},
            )
            if outcome.succeeded:
                expired_count += 1
                logger.info(f"Expired stale job {job.job_id} (was {job.status})")
            else:
                skipped_count += 1
                logger.debug(f"Job {job.job_id} was not expired: reason={outcome.reason}")
    return expired_count, skipped_count
