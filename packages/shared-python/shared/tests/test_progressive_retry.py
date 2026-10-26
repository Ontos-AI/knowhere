"""Enhancement retry only renews failed work and leaves readable snapshots intact."""

import pytest

from shared.core.exceptions.domain_exceptions import ConflictException
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    ProgressiveIngestionState,
)
from shared.services.jobs.progressive_retry import renew_failed_enhancement


def test_retry_preserves_complete_work_and_renews_failed_claim() -> None:
    state = ProgressiveIngestionState(
        enhancement=EnhancementProgress(status="failed", can_retry=True, attempt=2),
        stages={
            "structure": {"status": "complete", "attempts": 1},
            "vector_repair": {
                "status": "failed",
                "attempts": 3,
                "claim_generation": "old",
                "pending_pages": [7],
            },
        },
    )
    renewed = renew_failed_enhancement(state, job_id="j")
    assert state.enhancement.status == "failed"
    assert renewed.enhancement.status == "pending"
    assert renewed.enhancement.attempt == 3
    assert not renewed.enhancement.can_retry
    stages = renewed.model_dump()["stages"]
    assert stages["structure"] == {"status": "complete", "attempts": 1}
    assert stages["vector_repair"]["attempts"] == 0
    assert stages["vector_repair"]["claim_generation"] is None
    assert stages["vector_repair"]["pending_pages"] == [7]


def test_duplicate_retry_keeps_current_generation() -> None:
    active = ProgressiveIngestionState(
        enhancement=EnhancementProgress(status="running", attempt=3)
    )
    assert renew_failed_enhancement(active, job_id="j") == active


@pytest.mark.parametrize("status,can_retry", [("complete", False), ("failed", False)])
def test_retry_rejects_ineligible_state(status: str, can_retry: bool) -> None:
    state = ProgressiveIngestionState.model_validate(
        {"enhancement": {"status": status, "can_retry": can_retry}}
    )
    with pytest.raises(ConflictException):
        renew_failed_enhancement(state, job_id="j")
