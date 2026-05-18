from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from sqlalchemy.orm import Session

from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication import SyncJobPublicationFinalizer
from shared.services.jobs.lifecycle.result_writer import SyncJobResultWriter
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox


@dataclass(frozen=True)
class JobSuccessResponse:
    status: Literal["success", "failed"]
    job_id: str
    stored_count: int | None = None
    reason: str | None = None

    @classmethod
    def completed(cls, *, job_id: str, stored_count: int) -> JobSuccessResponse:
        return cls(status="success", job_id=job_id, stored_count=stored_count)

    @classmethod
    def state_transition_failed(cls, *, job_id: str) -> JobSuccessResponse:
        return cls(status="failed", job_id=job_id, reason="state_transition_failed")

    def should_commit(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "status": self.status,
            "job_id": self.job_id,
        }
        if self.stored_count is not None:
            response["stored_count"] = self.stored_count
        if self.reason:
            response["reason"] = self.reason
        return response


@dataclass(frozen=True)
class JobSuccessFinalization:
    response: JobSuccessResponse
    post_commit_effects: PostCommitEffectPlan


class SyncJobSuccessFinalizer:
    """Finalize successful Jobs inside the lifecycle transaction."""

    def __init__(
        self,
        *,
        state_machine: SyncStateMachineService | None = None,
        result_writer: SyncJobResultWriter | None = None,
        publication_finalizer: SyncJobPublicationFinalizer | None = None,
        webhook_outbox: SyncJobWebhookOutbox | None = None,
    ) -> None:
        self._state_machine = state_machine or SyncStateMachineService()
        self._result_writer = result_writer or SyncJobResultWriter()
        self._publication_finalizer = (
            publication_finalizer or SyncJobPublicationFinalizer()
        )
        self._webhook_outbox = webhook_outbox or SyncJobWebhookOutbox()

    def finalize(
        self,
        db: Session,
        *,
        job_id: str,
        result_s3_key: str,
        checksum: str,
        zip_size: int,
        chunks: list[dict[str, Any]],
        stored_count: int,
        delivery_mode: str,
        section_summaries: dict[str, str] | None,
    ) -> JobSuccessFinalization:
        job_result = self._result_writer.upsert_job_result(
            db,
            job_id,
            delivery_mode,
            inline_payload={"checksum": checksum},
            result_s3_key=result_s3_key,
            result_size=zip_size,
        )
        self._result_writer.replace_chunks(db, job_result.id, chunks)
        publication_outcome = self._publication_finalizer.publish_result(
            db,
            job_id=job_id,
            job_result_id=job_result.id,
            chunks=chunks,
            section_summaries=section_summaries,
        )

        transition_outcome = self._state_machine.mark_completed_outcome(
            db,
            job_id,
            result_metadata={
                "storage_completed": True,
                "stored_count": stored_count,
                "delivery_mode": delivery_mode,
            },
        )
        if not transition_outcome.succeeded:
            logger.error(
                f"Job {job_id} mark_completed transition failed: "
                f"reason={transition_outcome.reason}"
            )
            return JobSuccessFinalization(
                response=JobSuccessResponse.state_transition_failed(job_id=job_id),
                post_commit_effects=PostCommitEffectPlan.none(),
            )

        webhook_event = self._webhook_outbox.create_event(
            db,
            job_id=job_id,
            event_type="job.completed",
        )
        return JobSuccessFinalization(
            response=JobSuccessResponse.completed(
                job_id=job_id,
                stored_count=stored_count,
            ),
            post_commit_effects=PostCommitEffectPlan.from_success(
                cache_invalidation=publication_outcome.cache_invalidation,
                webhook_event_id=webhook_event.event_id if webhook_event else None,
            ),
        )
