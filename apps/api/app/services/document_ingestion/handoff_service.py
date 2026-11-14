from __future__ import annotations

from app.services.document_ingestion.worker_dispatcher import (
    DocumentIngestionWorkerDispatcher,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    UnavailableException,
    ValidationException,
)
from shared.core.state_machine.service import AsyncStateMachineService
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.source_integrity import SourceIntegrity
from shared.services.jobs.source_upload_deadline import (
    UPLOAD_EXPIRED_CODE,
    UPLOAD_EXPIRED_MESSAGE,
    UPLOAD_INTEGRITY_MISMATCH_CODE,
    UPLOAD_INTEGRITY_MISMATCH_MESSAGE,
    classify_guarded_source_upload,
)
from shared.services.jobs.upload_parse_dispatch import UploadParseOutbox
from shared.services.storage.file_upload_service import FileUploadService

_DOCUMENT_PARSE_JOB_TYPE = "document_ingestion"
_LEGACY_DOCUMENT_PARSE_JOB_TYPE = "kb_management"
_SUPPORTED_DOCUMENT_PARSE_JOB_TYPES = frozenset(
    {
        _DOCUMENT_PARSE_JOB_TYPE,
        _LEGACY_DOCUMENT_PARSE_JOB_TYPE,
    }
)


class DocumentIngestionHandoffService:
    """Advance uploaded Document Ingestion Jobs into worker parsing."""

    def __init__(
        self,
        *,
        state_machine: AsyncStateMachineService | None = None,
        worker_dispatcher: DocumentIngestionWorkerDispatcher | None = None,
    ) -> None:
        self._state_machine = state_machine or AsyncStateMachineService()
        self._worker_dispatcher = (
            worker_dispatcher or DocumentIngestionWorkerDispatcher()
        )

    async def start_uploaded_file_workflow(
        self,
        db: AsyncSession,
        *,
        job: Job,
        user_id: str,
        trigger: str,
    ) -> None:
        if job.job_type not in _SUPPORTED_DOCUMENT_PARSE_JOB_TYPES:
            raise ValidationException(
                user_message="Unsupported job type",
                violations=[
                    {
                        "field": "job_type",
                        "description": f"Job type '{job.job_type}' is not supported",
                    }
                ],
            )

        # Upload completion can arrive through both the S3 notification and the
        # confirm-upload endpoint.  Once either path has moved the job out of
        # waiting-file, the other path must be a no-op instead of dispatching a
        # second worker task for the same logical job.
        if job.status != JobStatus.WAITING_FILE.value:
            logger.info(
                "Upload handoff already completed: "
                f"job_id={job.job_id}, status={job.status}"
            )
            return

        source_integrity = JobMetadataHelper.get_source_integrity(job.job_metadata)
        if source_integrity is not None and not await self._verify_guarded_source(
            db, job=job, source_integrity=source_integrity
        ):
            return

        job_id = job.job_id
        outcome = await self._state_machine.transition_outcome(
            db,
            job_id,
            JobStatus.PENDING.value,
            trigger,
            auto_commit=False,
            expected_from_state=JobStatus.WAITING_FILE.value,
        )
        if not outcome.succeeded:
            await db.rollback()
            # A concurrent trigger may have advanced the job after our read.
            if outcome.reason == "invalid_transition" and outcome.from_state != (
                JobStatus.WAITING_FILE.value
            ):
                logger.info(
                    "Upload handoff won by another trigger: "
                    f"job_id={job_id}, status={outcome.from_state}"
                )
                return
            logger.warning(
                "Upload handoff transition rejected: "
                f"job_id={job_id}, reason={outcome.reason}"
            )
            raise UnavailableException(
                internal_message=(
                    f"Could not advance uploaded job {job_id} to pending: "
                    f"{outcome.reason}"
                ),
                retry_after=settings.DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN,
                user_message="Job state is still settling. Retrying shortly.",
            )

        claim = await db.run_sync(
            UploadParseOutbox.stage, job_id=job_id, user_id=user_id
        )
        await db.commit()
        await self._state_machine.publish_status_cache(job_id, JobStatus.PENDING.value)

        published = False
        try:
            await self._worker_dispatcher.start_uploaded_file_parse(
                job_id=job_id, user_id=user_id
            )
            published = True
        except Exception as exc:
            logger.warning(
                f"Upload parse publication will be retried: job_id={job_id}, error={exc}"
            )

        await db.run_sync(UploadParseOutbox.finish, claim, published=published)
        await db.commit()

    async def _verify_guarded_source(
        self, db: AsyncSession, *, job: Job, source_integrity: SourceIntegrity
    ) -> bool:
        job_id = job.job_id
        if not job.s3_key:
            raise ValidationException(
                user_message="Job is missing S3 key information",
                violations=[{"field": "s3_key", "description": "S3 key not set for this job"}],
            )
        verification = await FileUploadService().verify_upload_integrity(
            job.s3_key, source_integrity
        )
        result = classify_guarded_source_upload(job.created_at, verification)

        # Storage checks happen outside the row lock. The terminal decision and
        # parse handoff both recheck waiting-file under that same lock.
        locked_job = await self._lock_waiting_job(db, job_id)
        if locked_job is None:
            return False
        if result == "expired":
            await self._mark_locked_upload_failed(
                db, job=locked_job, code=UPLOAD_EXPIRED_CODE, message=UPLOAD_EXPIRED_MESSAGE
            )
            raise ConflictException(
                user_message="The source upload has expired.",
                reason="ABORTED",
                resource="Job",
                resource_id=job_id,
            )
        if result == "missing":
            raise ValidationException(
                user_message="S3 file does not exist, please upload the file first",
                violations=[{"field": "file", "description": "File not found in S3"}],
            )
        if result == "mismatch":
            await self._mark_locked_upload_failed(
                db,
                job=locked_job,
                code=UPLOAD_INTEGRITY_MISMATCH_CODE,
                message=UPLOAD_INTEGRITY_MISMATCH_MESSAGE,
            )
            raise ValidationException(
                user_message=UPLOAD_INTEGRITY_MISMATCH_MESSAGE,
                violations=[{
                    "field": "source_integrity",
                    "description": "Stored length or SHA-256 does not match",
                }],
            )
        return True

    @staticmethod
    async def _lock_waiting_job(db: AsyncSession, job_id: str) -> Job | None:
        return await db.scalar(
            select(Job)
            .where(Job.job_id == job_id, Job.status == JobStatus.WAITING_FILE.value)
            .with_for_update()
        )

    async def mark_upload_expired(self, db: AsyncSession, *, job: Job) -> None:
        locked_job = await self._lock_waiting_job(db, job.job_id)
        if locked_job is not None:
            await self._mark_locked_upload_failed(
                db, job=locked_job, code=UPLOAD_EXPIRED_CODE, message=UPLOAD_EXPIRED_MESSAGE
            )

    async def _mark_locked_upload_failed(
        self, db: AsyncSession, *, job: Job, code: str, message: str,
    ) -> None:
        job_id = job.job_id
        metadata = {"error_code": code, "error_message": message}
        job.error_code = code
        job.error_message = message
        outcome = await self._state_machine.transition_outcome(
            db,
            job_id,
            JobStatus.FAILED.value,
            "mark_failed",
            metadata=metadata,
            auto_commit=False,
            expected_from_state=JobStatus.WAITING_FILE.value,
        )
        if not outcome.succeeded:
            await db.rollback()
            raise UnavailableException(
                internal_message=f"Could not fail upload for job_id={job_id}, reason={outcome.reason}",
                retry_after=settings.DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN,
                user_message="Job state is still settling. Retrying shortly.",
            )
        await db.commit()
        await self._state_machine.publish_status_cache(job_id, JobStatus.FAILED.value, metadata)
