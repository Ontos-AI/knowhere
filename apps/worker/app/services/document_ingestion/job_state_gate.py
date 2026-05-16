"""
Worker-side Document Ingestion state gate.

Keeps the worker-specific policy for when a parse task is allowed to move a
Job into ``running`` while delegating the actual transition to the shared sync
state machine service.
"""

from typing import Any

from loguru import logger
from sqlalchemy import select

from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
    UnavailableException,
    WorkerHandlingException,
)
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.core.state_machine.states import JobStatus, is_terminal_state
from shared.models.database.job import Job


def mark_job_running(job_id: str, redis_service: Any) -> bool:
    """Transition a Job from pending to running before parse execution."""
    state_machine = SyncStateMachineService(redis_service)

    with get_sync_db_context() as db:
        job_result = db.execute(select(Job).where(Job.job_id == job_id))
        job = job_result.scalar_one_or_none()

        if job is None:
            raise NotFoundException(
                resource="Job",
                resource_id=job_id,
                internal_message=f"Job not found while starting parse_task: {job_id}",
            )

        current_state = job.status

        if current_state == JobStatus.RUNNING.value:
            logger.info(
                f"Job already running (likely redelivery), deferring to lock: {job_id}"
            )
            return True

        if current_state == JobStatus.WAITING_FILE.value:
            raise UnavailableException(
                internal_message=(
                    f"Parse task started before upload transition completed for job {job_id}; "
                    f"current_state={current_state}"
                ),
                retry_after=settings.KB_TASK_RETRY_COUNTDOWN,
                user_message="Job is not ready for processing yet. Retrying shortly.",
            )

        if is_terminal_state(current_state):
            logger.warning(
                f"Skipping parse_task for terminal job state: job_id={job_id}, "
                f"current_state={current_state}"
            )
            return False

        if current_state != JobStatus.PENDING.value:
            raise WorkerHandlingException(
                user_message="The job is not ready for processing",
                internal_message=(
                    f"Refusing to start parse_task for job {job_id}: "
                    f"expected pending or running, found {current_state}"
                ),
            )

        if not state_machine.transition(
            db,
            job_id,
            JobStatus.RUNNING.value,
            "start_processing",
            operator_type="system",
        ):
            raise UnavailableException(
                internal_message=(
                    f"Failed to transition job {job_id} from pending to running; "
                    "the state may have changed concurrently"
                ),
                retry_after=settings.KB_TASK_RETRY_COUNTDOWN,
                user_message="Job state is still settling. Retrying shortly.",
            )

        return True
