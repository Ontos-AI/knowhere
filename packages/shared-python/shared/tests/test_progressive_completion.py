"""Final artifact publication and its outbox event form one logical commit."""

from unittest.mock import Mock
import pytest

from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import StoredSnapshot
from shared.services.jobs.progressive_completion import commit_final_completion
from shared.services.jobs.progressive_ingestion import (
    initialize_baseline_state,
    read_progressive_state,
)
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox


def test_final_completion_records_identity_and_emits_only_once() -> None:
    baseline = StoredSnapshot(
        snapshot_id="s0",
        stage="baseline",
        job_id="j",
        job_result_id="r",
        document_id="d",
        checksum="a" * 64,
        archive_key="results/j/s0.zip",
    )
    result = JobResult(
        id="r",
        job_id="j",
        document_id="d",
        delivery_mode="url",
        document_metadata={
            "progressive": initialize_baseline_state(
                snapshot=baseline, missing_pages=[]
            ).model_dump()
        },
    )
    final = baseline.model_copy(
        update={
            "snapshot_id": "s1",
            "stage": "final",
            "archive_key": "results/j/s1.zip",
        }
    )
    outbox = Mock(spec=SyncJobWebhookOutbox)
    db = Mock()
    commit_final_completion(
        db, job_id="j", job_result=result, snapshot=final, outbox=outbox
    )
    state = read_progressive_state(result.document_metadata)
    assert state is not None
    assert state.enhancement.status == "complete"
    assert state.current_snapshot_id == "s1"
    assert state.snapshots[0] == baseline
    assert result.result_s3_key == "results/j/s1.zip"
    outbox.create_event.assert_called_once()
    assert (
        outbox.create_event.call_args.kwargs["event_type"] == "job.enrichment.completed"
    )
    assert outbox.create_event.call_args.kwargs["extra_payload"]["snapshot_id"] == "s1"
    assert (
        commit_final_completion(
            db, job_id="j", job_result=result, snapshot=final, outbox=outbox
        )
        is None
    )
    outbox.create_event.assert_called_once()


def test_final_webhook_is_a_success_event() -> None:
    db = Mock()
    db.execute.return_value.one_or_none.return_value = (
        "https://callback.example",
        True,
    )
    SyncJobWebhookOutbox().create_event(
        db, job_id="j", event_type="job.enrichment.completed"
    )
    event = db.add.call_args.args[0]
    assert event.payload["status"] == "completed"
    assert "completed_at" in event.payload


def test_baseline_dispatches_recovery_without_waiting_for_periodic_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.jobs.lifecycle import post_commit_effects

    recover = Mock()
    monkeypatch.setattr(
        post_commit_effects, "enqueue_page_memory_structure_after_commit", Mock()
    )
    monkeypatch.setattr(
        post_commit_effects,
        "enqueue_progressive_recovery_after_commit",
        recover,
        raising=False,
    )
    plan = post_commit_effects.PostCommitEffectPlan(structure_job_result_ids=("r",))
    post_commit_effects.SyncJobPostCommitEffectRunner().run(plan)
    recover.assert_called_once_with("r")
