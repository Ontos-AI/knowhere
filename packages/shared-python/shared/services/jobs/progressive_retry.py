"""Renew failed enhancement work without repeating ingestion or settlement."""

from shared.core.exceptions.domain_exceptions import ConflictException
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    ProgressiveIngestionState,
)


def renew_failed_enhancement(
    state: ProgressiveIngestionState,
    *,
    job_id: str,
) -> ProgressiveIngestionState:
    if state.enhancement.status in {"pending", "running"}:
        return state
    if not state.enhancement.can_retry:
        raise ConflictException(
            user_message="Search enhancement is not eligible for retry.",
            resource="Job",
            resource_id=job_id,
        )
    renewed: ProgressiveIngestionState = state.model_copy(deep=True)
    renewed.enhancement = EnhancementProgress(attempt=state.enhancement.attempt + 1)
    for stage in renewed.stages.values():
        if stage.get("status") != "failed":
            continue
        stage.update(
            status="pending",
            attempts=0,
            claim_generation=None,
            claimed_at=None,
            error=None,
            next_retry_at=None,
        )
    return renewed
