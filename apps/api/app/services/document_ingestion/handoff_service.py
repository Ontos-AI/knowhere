from __future__ import annotations

from typing import Protocol

from app.services.document_ingestion.worker_dispatcher import (
    DocumentIngestionWorkerDispatcher,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    UnavailableException,
    ValidationException,
)
from shared.core.state_machine.service import AsyncStateMachineService
from shared.core.state_machine.states import JobStatus

_DOCUMENT_PARSE_JOB_TYPE = "document_ingestion"
_LEGACY_DOCUMENT_PARSE_JOB_TYPE = "kb_management"
_SUPPORTED_DOCUMENT_PARSE_JOB_TYPES = frozenset(
    {
        _DOCUMENT_PARSE_JOB_TYPE,
        _LEGACY_DOCUMENT_PARSE_JOB_TYPE,
    }
)


class _UploadedFileJob(Protocol):
    job_id: str
    job_type: str


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
        job: _UploadedFileJob,
        user_id: str,
        trigger: str,
    ) -> None:
        if job.job_type not in _SUPPORTED_DOCUMENT_PARSE_JOB_TYPES:
            raise ValidationException(
                user_message="Unsupported job type",
                violations=[
                    {
                        "field": "job_type",
                        "description": (
                            f"Job type '{job.job_type}' is not supported"
                        ),
                    }
                ],
            )

        outcome = await self._state_machine.transition_outcome(
            db,
            job.job_id,
            JobStatus.PENDING.value,
            trigger,
            None,
            "system",
        )
        if not outcome.succeeded:
            logger.warning(
                "Upload handoff transition rejected: "
                f"job_id={job.job_id}, reason={outcome.reason}"
            )
            raise UnavailableException(
                internal_message=(
                    f"Could not advance uploaded job {job.job_id} to pending: "
                    f"{outcome.reason}"
                ),
                retry_after=settings.DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN,
                user_message="Job state is still settling. Retrying shortly.",
            )

        await self._worker_dispatcher.start_uploaded_file_parse(
            job_id=job.job_id,
            user_id=user_id,
        )

    async def mark_upload_expired(
        self,
        db: AsyncSession,
        *,
        job: _UploadedFileJob,
    ) -> None:
        outcome = await self._state_machine.mark_failed_outcome(
            db,
            job.job_id,
            "Upload expired: file was not uploaded within the allowed time window",
            error_code="UPLOAD_EXPIRED",
        )
        if not outcome.succeeded:
            logger.warning(
                "Upload expiry transition rejected: "
                f"job_id={job.job_id}, reason={outcome.reason}"
            )
