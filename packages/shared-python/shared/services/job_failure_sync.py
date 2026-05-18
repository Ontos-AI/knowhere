from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.response import build_standard_error_response
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.job import Job
from shared.services.billing.credits_sync_service import SyncCreditsService
from shared.services.job_post_commit_effects_sync import PostCommitEffectPlan
from shared.services.job_webhook_outbox_sync import SyncJobWebhookOutbox
from shared.utils.error_details import normalize_error_details


@dataclass(frozen=True)
class JobFailureFinalization:
    succeeded: bool
    post_commit_effects: PostCommitEffectPlan


class SyncJobFailureFinalizer:
    """Finalize failed Jobs inside the lifecycle transaction."""

    def __init__(
        self,
        *,
        state_machine: SyncStateMachineService | None = None,
        webhook_outbox: SyncJobWebhookOutbox | None = None,
        credits_service: SyncCreditsService | None = None,
    ) -> None:
        self._state_machine = state_machine or SyncStateMachineService()
        self._webhook_outbox = webhook_outbox or SyncJobWebhookOutbox()
        self._credits_service = credits_service or SyncCreditsService()

    def finalize(
        self,
        db: Session,
        *,
        job_id: str,
        error_message: str,
        error_code: str,
        error_details: dict[str, Any] | None,
        should_refund: bool,
    ) -> JobFailureFinalization:
        transition_outcome = self._state_machine.mark_failed_outcome(
            db,
            job_id,
            error_message,
            error_code=error_code,
            error_details=error_details,
        )
        if not transition_outcome.succeeded:
            logger.error(
                f"Job {job_id} mark_failed transition failed: "
                f"reason={transition_outcome.reason}"
            )
            return JobFailureFinalization(
                succeeded=False,
                post_commit_effects=PostCommitEffectPlan.none(),
            )

        if should_refund:
            self._try_refund_credits(db, job_id)

        normalized_error_details = normalize_error_details(error_details)
        webhook_event = self._webhook_outbox.create_event(
            db,
            job_id=job_id,
            event_type="job.failed",
            extra_payload={
                "error": build_standard_error_response(
                    code=error_code,
                    message=error_message,
                    request_id=job_id,
                    details=normalized_error_details,
                ),
            },
        )
        return JobFailureFinalization(
            succeeded=True,
            post_commit_effects=PostCommitEffectPlan.from_failure(
                webhook_event_id=webhook_event.id if webhook_event else None,
            ),
        )

    def _try_refund_credits(self, db: Session, job_id: str) -> None:
        try:
            result = db.execute(select(Job).where(Job.job_id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                return

            amount = getattr(job, "credits_charged", 0) or 0
            billing_status = getattr(job, "billing_status", "")
            if amount <= 0 or billing_status != "charged":
                return

            self._credits_service.refund_job_credits(
                session=db,
                user_id=str(job.user_id),
                amount=amount,
                job_id=job_id,
            )
            job.billing_status = "refunded"
            logger.info(f"Refunded {amount} credits for job {job_id}")
        except Exception as exc:
            logger.error(f"Credit refund failed for job {job_id}: {exc}")
