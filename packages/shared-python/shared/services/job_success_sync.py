from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.webhook import WebhookEvent
from shared.services.job_publication_sync import SyncJobPublicationFinalizer
from shared.services.job_result_sync import SyncJobResultWriter
from shared.services.job_webhook_outbox_sync import SyncJobWebhookOutbox


@dataclass(frozen=True)
class JobSuccessFinalization:
    response: dict[str, Any]
    cache_invalidation: dict[str, Any] | None
    webhook_event: WebhookEvent | None


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

        transition_ok = self._state_machine.mark_completed(
            db,
            job_id,
            result_metadata={
                "storage_completed": True,
                "stored_count": stored_count,
                "delivery_mode": delivery_mode,
            },
        )
        if not transition_ok:
            logger.error(f"Job {job_id} mark_completed transition failed")
            return JobSuccessFinalization(
                response={
                    "status": "failed",
                    "job_id": job_id,
                    "reason": "state_transition_failed",
                },
                cache_invalidation=None,
                webhook_event=None,
            )

        webhook_event = self._webhook_outbox.create_event(
            db,
            job_id=job_id,
            event_type="job.completed",
        )
        return JobSuccessFinalization(
            response={
                "status": "success",
                "job_id": job_id,
                "stored_count": stored_count,
            },
            cache_invalidation=publication_outcome.cache_invalidation,
            webhook_event=webhook_event,
        )

    def run_post_commit_actions(
        self,
        finalization: JobSuccessFinalization,
    ) -> None:
        self._publication_finalizer.invalidate_cache_after_commit(
            finalization.cache_invalidation,
        )
        self._webhook_outbox.enqueue_after_commit(finalization.webhook_event)
