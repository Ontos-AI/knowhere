from __future__ import annotations

from app.repositories.job_repository import JobRepository
from app.services.document_ingestion.handoff_service import (
    DocumentIngestionHandoffService,
)
from app.services.jobs import check_job_permission
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    JobOperationException,
    NotFoundException,
    PermissionDeniedException,
    ValidationException,
)
from shared.core.state_machine.states import JobStatus
from shared.services.storage.file_upload_service import FileUploadService


class DocumentIngestionConfirmationService:
    def __init__(
        self,
        *,
        job_repository: JobRepository | None = None,
        file_upload_service: FileUploadService | None = None,
        handoff_service: DocumentIngestionHandoffService | None = None,
    ) -> None:
        self._job_repository = job_repository or JobRepository()
        self._file_upload_service = file_upload_service or FileUploadService()
        self._handoff_service = handoff_service or DocumentIngestionHandoffService()

    async def confirm_upload(
        self,
        db: AsyncSession,
        *,
        job_id: str,
        user_id: str,
    ) -> dict[str, str]:
        try:
            job = await self._job_repository.get_job_by_id(db, job_id)
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
                        {
                            "field": "s3_key",
                            "description": "S3 key not set for this job",
                        }
                    ],
                )

            file_info = await self._file_upload_service.verify_s3_file_exists(job.s3_key)
            if not bool(file_info.get("exists")):
                raise ValidationException(
                    user_message="S3 file does not exist, please upload the file first",
                    violations=[
                        {"field": "file", "description": "File not found in S3"}
                    ],
                )

            await self._handoff_service.start_uploaded_file_workflow(
                db=db,
                job=job,
                user_id=user_id,
                trigger="manual_upload_completed",
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
