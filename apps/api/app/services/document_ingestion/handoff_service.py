from __future__ import annotations

from typing import Any

from app.services.knowledge.kb_orchestrator import KBOrchestrator
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    UnavailableException,
    ValidationException,
)
from shared.core.state_machine.service import AsyncStateMachineService
from shared.core.state_machine.states import JobStatus

_JOB_TYPE_KB_MANAGEMENT = "kb_management"


class DocumentIngestionHandoffService:
    """Advance uploaded Document Ingestion Jobs into worker parsing."""

    def __init__(
        self,
        *,
        state_machine: AsyncStateMachineService | None = None,
        orchestrator: KBOrchestrator | None = None,
    ) -> None:
        self._state_machine = state_machine or AsyncStateMachineService()
        self._orchestrator = orchestrator or KBOrchestrator()

    async def start_uploaded_file_workflow(
        self,
        db: AsyncSession,
        *,
        job: Any,
        user_id: str,
        trigger: str,
    ) -> None:
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
                retry_after=settings.KB_TASK_RETRY_COUNTDOWN,
                user_message="Job state is still settling. Retrying shortly.",
            )

        if job.job_type != _JOB_TYPE_KB_MANAGEMENT:
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

        await self._orchestrator.start_workflow(
            db=db,
            job_id=job.job_id,
            source_type="file",
            file_path=None,
            file_url=None,
            user_id=user_id,
        )

    async def mark_upload_expired(self, db: AsyncSession, *, job: Any) -> None:
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
