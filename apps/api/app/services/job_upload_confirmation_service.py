from __future__ import annotations

from typing import Optional

from app.repositories.job_repository import JobRepository
from app.services.job_read_service import check_job_permission
from app.services.knowledge.kb_orchestrator import KBOrchestrator
from app.services.state_machine import JobStateMachine
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    JobOperationException,
    NotFoundException,
    PermissionDeniedException,
    ValidationException,
)
from shared.core.state_machine.states import JobStatus
from shared.models.schemas.job import ConfirmUploadRequest
from shared.services.storage.file_upload_service import FileUploadService


async def transition_to_uploaded(
    db: AsyncSession,
    job_id: str,
    trigger: str = "manual_upload_completed",
) -> None:
    state_machine = JobStateMachine()
    await state_machine.transition(
        db, job_id, JobStatus.PENDING.value, trigger, None, "system"
    )


async def start_workflow_for_job(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    source_type: str,
    user_id: str,
    file_path: Optional[str] = None,
    file_url: Optional[str] = None,
) -> None:
    if job_type == "kb_management":
        orchestrator = KBOrchestrator()
        await orchestrator.start_workflow(
            db=db,
            job_id=job_id,
            source_type=source_type,
            file_path=file_path,
            file_url=file_url,
            user_id=user_id,
        )
        return

    raise ValidationException(
        user_message="Unsupported job type",
        violations=[
            {
                "field": "job_type",
                "description": f"Job type '{job_type}' is not supported",
            }
        ],
    )


async def confirm_job_upload(
    db: AsyncSession,
    *,
    job_id: str,
    request: ConfirmUploadRequest | None,
    user_id: str,
) -> dict[str, str]:
    del request

    try:
        job_repo = JobRepository()
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, user_id, job_id)
        assert job is not None

        logger.info(f"Confirm upload - Job {job_id} current status: {job.status}")
        if job.status not in [JobStatus.PENDING.value, JobStatus.WAITING_FILE.value]:
            logger.info(f"Job {job_id} already processed, status: {job.status}")
            return {"message": "Job status already updated"}

        if not job.s3_key:
            raise ValidationException(
                user_message="Job is missing S3 key information",
                violations=[
                    {"field": "s3_key", "description": "S3 key not set for this job"}
                ],
            )

        upload_service = FileUploadService()
        file_info = await upload_service.verify_s3_file_exists(job.s3_key)

        if not file_info.get("exists"):
            raise ValidationException(
                user_message="S3 file does not exist, please upload the file first",
                violations=[{"field": "file", "description": "File not found in S3"}],
            )

        await transition_to_uploaded(db, job_id)
        await start_workflow_for_job(
            db=db,
            job_id=job_id,
            job_type=job.job_type,
            source_type="file",
            user_id=user_id,
        )

        return {"message": "File upload confirmed; processing started"}

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except ValidationException:
        raise
    except Exception as exc:
        logger.error(f"Failed to confirm upload: {exc}")
        raise JobOperationException(
            internal_message=f"Failed to confirm upload: {str(exc)}"
        )
