"""
Stale Job Sweeper — periodic Celery Beat task.

Scans for jobs stuck in any active state (pending, waiting-file, running, converting)
beyond the global JOB_EXPIRE_SECONDS threshold and marks them as failed.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy import select as sa_select

from shared.core.celery_app import get_celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.core.state_machine.states import (
    TERMINAL_STATES,
    JobStatus,
)
from shared.models.database.job import Job
from shared.services.redis.periodic_task_lock import periodic_task_lock

celery_app = get_celery_app()

JOB_EXPIRED_ERROR_MESSAGE = (
    "Job timed out: execution or upload exceeded maximum allowed duration"
)
JOB_EXPIRED_ERROR_CODE = "JOB_EXPIRED"

# Matches the beat_schedule period in celery_app.py
_TASK_PERIOD_SECONDS = 1800


@celery_app.task(name="app.core.tasks.stale_job_sweeper.expire_stale_jobs")
def expire_stale_jobs() -> dict:
    """Expire jobs stuck in non-terminal states beyond ``JOB_EXPIRE_SECONDS``.

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

        waiting_max_age: int = settings.JOB_WAITING_EXPIRE_SECONDS
        processing_max_age: int = settings.JOB_PROCESSING_EXPIRE_SECONDS

        if waiting_max_age <= 0 or processing_max_age <= 0:
            return {"status": "skipped", "reason": "Expiration config <= 0"}

        # Timeouts: 4 hours (default) for processing, 2 hours (default) for waiting
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        long_cutoff = now - timedelta(seconds=processing_max_age)
        default_cutoff = now - timedelta(seconds=waiting_max_age)

        expired_count = 0
        skipped_count = 0

        try:
            state_machine = SyncStateMachineService()

            with get_sync_db_context() as db:
                stmt = (
                    sa_select(Job)
                    .where(
                        Job.status.notin_(TERMINAL_STATES),
                        or_(
                            and_(
                                Job.status.in_(
                                    [
                                        JobStatus.RUNNING.value,
                                        JobStatus.CONVERTING.value,
                                    ]
                                ),
                                Job.updated_at < long_cutoff,
                            ),
                            and_(
                                Job.status.notin_(
                                    [
                                        JobStatus.RUNNING.value,
                                        JobStatus.CONVERTING.value,
                                    ]
                                ),
                                Job.updated_at < default_cutoff,
                            ),
                        ),
                    )
                    .order_by(Job.updated_at)
                    .limit(200)
                )
                expired_jobs = db.execute(stmt).scalars().all()

                if not expired_jobs:
                    logger.debug("Stale job sweeper: no expired jobs found")
                    return {"status": "success", "expired": 0, "skipped": 0}

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
                        logger.info(
                            f"Expired stale job {job.job_id} (was {job.status})"
                        )
                    else:
                        skipped_count += 1
                        logger.debug(
                            f"Job {job.job_id} was not expired: reason={outcome.reason}"
                        )

            if expired_count > 0:
                logger.info(
                    f"Stale job sweeper completed: expired={expired_count}, "
                    f"skipped={skipped_count}"
                )
            return {
                "status": "success",
                "expired": expired_count,
                "skipped": skipped_count,
            }

        except Exception as e:
            logger.error(f"Stale job sweeper failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
