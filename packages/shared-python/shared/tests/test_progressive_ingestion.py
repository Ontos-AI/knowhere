"""Reader snapshots keep artifact identity separate from the business revision."""

import pytest
from pydantic import ValidationError
from datetime import datetime, timezone
from shared.models.schemas.job import JobResultResponse

from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    ProgressiveIngestionState,
    StoredSnapshot,
)
from shared.services.jobs.progressive_ingestion import (
    initialize_baseline_state,
    read_progressive_state,
    write_progressive_state,
)


def test_legacy_result_has_no_progressive_state() -> None:
    assert read_progressive_state({"enrichment_status": "text"}) is None


def test_full_result_omits_progressive_fields_in_public_json() -> None:
    now = datetime.now(timezone.utc)
    response = JobResultResponse(
        job_id="j",
        status="done",
        source_type="file",
        created_at=now,
        result_url_expires_at=now,
    )
    payload = response.model_dump(mode="json")
    assert "enhancement" not in payload
    assert "snapshots" not in payload
    assert "current_snapshot_id" not in payload


def test_snapshot_round_trip_preserves_stage_claims_without_mutating_metadata() -> None:
    snapshot = StoredSnapshot(
        snapshot_id="baseline_1",
        stage="baseline",
        job_id="job_1",
        job_result_id="result_1",
        document_id="doc_1",
        checksum="a" * 64,
        archive_key="results/job_1/baseline_1.zip",
    )
    state = ProgressiveIngestionState(
        snapshots=[snapshot],
        current_snapshot_id=snapshot.snapshot_id,
        vector_repair={"missing_pages": [7], "generation": "claim_1"},
    )
    original: dict[str, object] = {"source": "kept"}
    stored = write_progressive_state(original, state)
    assert "progressive" not in original
    restored = read_progressive_state(stored)
    assert restored == state
    assert restored is not None
    assert restored.model_dump()["vector_repair"]["missing_pages"] == [7]


def test_retry_is_only_advertised_for_terminal_failure() -> None:
    with pytest.raises(ValidationError):
        EnhancementProgress(status="running", can_retry=True)
    assert EnhancementProgress(status="failed", can_retry=True).can_retry


def test_current_snapshot_must_identify_a_committed_artifact() -> None:
    with pytest.raises(ValidationError):
        ProgressiveIngestionState(current_snapshot_id="not_uploaded")


def test_snapshot_checksum_requires_sha256() -> None:
    with pytest.raises(ValidationError):
        StoredSnapshot(
            snapshot_id="s",
            stage="baseline",
            job_id="j",
            job_result_id="r",
            document_id="d",
            checksum="etag",
            archive_key="results/s.zip",
        )


def test_baseline_records_missing_vectors_without_blocking_reader() -> None:
    snapshot = StoredSnapshot(
        snapshot_id="baseline_1",
        stage="baseline",
        job_id="job_1",
        job_result_id="result_1",
        document_id="doc_1",
        checksum="a" * 64,
        archive_key="results/job_1/baseline_1.zip",
    )
    state = initialize_baseline_state(snapshot=snapshot, missing_pages=[7, 3, 7])
    assert state.current_snapshot_id == "baseline_1"
    assert state.enhancement.status == "pending"
    assert state.model_dump()["stages"]["vector_repair"]["pending_pages"] == [3, 7]
