"""Read and replace progressive metadata without changing unrelated fields."""

from collections.abc import Mapping
from datetime import timedelta

from shared.models.schemas.progressive_ingestion import (
    ProgressiveIngestionState,
    PublishedSnapshot,
    StoredSnapshot,
)
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.utc_now import utc_now_naive


def initialize_baseline_state(
    *,
    snapshot: StoredSnapshot,
    missing_pages: list[int],
) -> ProgressiveIngestionState:
    pending_pages: list[int] = sorted({page for page in missing_pages if page > 0})
    return ProgressiveIngestionState(
        publication_mode="final",
        snapshots=[snapshot],
        current_snapshot_id=snapshot.snapshot_id,
        stages={
            "structure": {"status": "pending", "attempts": 0},
            "vector_repair": {
                "status": "pending" if pending_pages else "complete",
                "attempts": 0,
                "pending_pages": pending_pages,
            },
            "finalization": {"status": "pending", "attempts": 0},
        },
    )


def read_progressive_state(
    metadata: Mapping[str, object] | None,
) -> ProgressiveIngestionState | None:
    if not metadata or not isinstance(metadata.get("progressive"), dict):
        return None
    return ProgressiveIngestionState.model_validate(metadata["progressive"])


def write_progressive_state(
    metadata: Mapping[str, object] | None,
    state: ProgressiveIngestionState,
) -> dict[str, object]:
    return {**(metadata or {}), "progressive": state.model_dump(mode="json")}


def publish_snapshot_descriptors(
    state: ProgressiveIngestionState,
    storage: JobFileStorage,
) -> list[PublishedSnapshot]:
    published: list[PublishedSnapshot] = []
    for snapshot in state.snapshots:
        capability = storage.generate_download_url(
            snapshot.archive_key,
            bucket=storage.results_bucket,
        )
        expires_at = utc_now_naive() + timedelta(
            seconds=int(capability.get("expires_in", 3600))
        )
        published.append(
            PublishedSnapshot(
                **snapshot.model_dump(exclude={"archive_key"}),
                result_url=capability["download_url"],
                result_url_expires_at=expires_at.isoformat() + "Z",
            )
        )
    return published
