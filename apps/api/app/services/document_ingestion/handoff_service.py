from __future__ import annotations

from typing import Any

from app.services.knowledge.kb_orchestrator import KBOrchestrator
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import ValidationException
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
        await self._state_machine.transition(
            db,
            job.job_id,
            JobStatus.PENDING.value,
            trigger,
            None,
            "system",
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
        await self._state_machine.mark_failed(
            db,
            job.job_id,
            "Upload expired: file was not uploaded within the allowed time window",
            error_code="UPLOAD_EXPIRED",
        )
