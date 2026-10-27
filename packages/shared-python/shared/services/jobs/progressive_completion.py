"""Commit the final artifact and its completion notification together."""

from sqlalchemy.orm import Session

from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    StoredSnapshot,
)
from shared.services.jobs.lifecycle.webhook_outbox import (
    SyncJobWebhookOutbox,
    WebhookOutboxEvent,
)
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)


def commit_final_completion(
    db: Session,
    *,
    job_id: str,
    job_result: JobResult,
    snapshot: StoredSnapshot,
    outbox: SyncJobWebhookOutbox | None = None,
) -> WebhookOutboxEvent | None:
    """Caller holds the result/revision claim lock and commits this transaction.

    Upload must finish first. Replays observe the committed final descriptor and
    do not create another logical event. The outbox survives dispatch failure.
    """
    state = read_progressive_state(job_result.document_metadata)
    if state is None:
        raise ValueError("Final enhancement requires a committed baseline")
    if any(existing.stage == "final" for existing in state.snapshots):
        return None
    if (
        snapshot.stage != "final"
        or snapshot.job_id != job_id
        or snapshot.job_result_id != job_result.id
        or snapshot.document_id != job_result.document_id
    ):
        raise ValueError("Final snapshot does not belong to the claimed revision")
    state.snapshots = [*state.snapshots, snapshot]
    state.current_snapshot_id = snapshot.snapshot_id
    state.enhancement = EnhancementProgress(
        status="complete", attempt=state.enhancement.attempt
    )
    job_result.document_metadata = {
        **write_progressive_state(job_result.document_metadata, state),
        "enrichment_status": "complete",
    }
    job_result.result_s3_key = snapshot.archive_key
    job_result.inline_payload = {
        **(job_result.inline_payload or {}),
        "checksum": snapshot.checksum,
    }
    return (outbox or SyncJobWebhookOutbox()).create_event(
        db,
        job_id=job_id,
        event_type="job.enrichment.completed",
        extra_payload={
            "job_result_id": snapshot.job_result_id,
            "document_id": snapshot.document_id,
            "snapshot_id": snapshot.snapshot_id,
            "checksum": snapshot.checksum,
        },
    )
