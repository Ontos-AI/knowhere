"""Process storage upload-complete events into job workflow handoffs."""
from __future__ import annotations

import os

from app.repositories.job_repository import JobRepository
from app.services.knowledge.kb_orchestrator import KBOrchestrator
from app.services.state_machine import JobStateMachine
from loguru import logger

from shared.core.database import get_db_context
from shared.core.state_machine.states import JobStatus
from shared.models.schemas.s3_event import S3Event


def extract_job_id_from_s3_key(s3_key: str) -> str | None:
    if not s3_key.startswith("uploads/"):
        return None

    filename = s3_key[8:]
    return os.path.splitext(filename)[0]


async def process_upload_events(s3_event: S3Event) -> None:
    try:
        upload_events = s3_event.get_upload_events()
        job_repo = JobRepository()

        for event in upload_events:
            s3_key = event.object_key or event.s3.get("object", {}).get("key")
            if not s3_key:
                continue

            job_id = extract_job_id_from_s3_key(s3_key)
            if not job_id:
                logger.warning(f"Could not extract job_id from S3 key: {s3_key}")
                continue

            logger.info(f"Processing S3 upload event: {s3_key} -> job_id={job_id}")

            async with get_db_context() as db:
                job = await job_repo.get_job_by_id(db, job_id)
                if not job:
                    logger.warning(f"No job found for upload event: {job_id}")
                    continue

                if job.status != "waiting-file":
                    logger.info(
                        f"Job {job_id} is not in waiting-file status: {job.status}"
                    )
                    continue

                from shared.core.config import settings
                from shared.core.state_machine.states import is_job_expired

                if is_job_expired(job.updated_at, settings.JOB_WAITING_EXPIRE_SECONDS):
                    logger.warning(f"Job {job_id} upload expired, marking failed")
                    state_machine = JobStateMachine()
                    await state_machine.mark_failed(
                        db,
                        job_id,
                        "Upload expired: file was not uploaded within the allowed time window",
                        error_code="UPLOAD_EXPIRED",
                    )
                    continue

                state_machine = JobStateMachine()
                await state_machine.transition(
                    db,
                    job_id,
                    JobStatus.PENDING.value,
                    "s3_upload_completed",
                    None,
                    "system",
                )

                if job.job_type == "kb_management":
                    orchestrator = KBOrchestrator()
                    await orchestrator.start_workflow(
                        db=db,
                        job_id=job_id,
                        source_type="file",
                        file_path=None,
                        file_url=None,
                        user_id=str(job.user_id),
                    )
                else:
                    logger.warning(
                        f"Unsupported job type for upload event: {job.job_type}, job_id={job_id}"
                    )

                logger.info(f"Triggered processing for job {job_id}")

    except Exception as exc:
        logger.error(f"Failed to process upload events: {exc}")
        raise
