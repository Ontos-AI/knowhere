"""Delayed initial webhooks must continue to identify their baseline artifact."""

from unittest.mock import Mock

from shared.models.database.job_result import JobResult
from shared.services.jobs.result_delivery import JobResultDeliveryResolver


def test_late_baseline_event_keeps_its_archive_after_final_publication() -> None:
    storage = Mock()
    storage.results_bucket = "results"
    storage.generate_download_url.side_effect = lambda key, **kwargs: {
        "download_url": f"https://storage.example/{key}",
        "expires_in": 3600,
    }
    snapshots = [
        {
            "snapshot_id": identity,
            "stage": stage,
            "job_id": "j",
            "job_result_id": "r",
            "document_id": "d",
            "checksum": checksum * 64,
            "archive_key": f"results/j/{identity}.zip",
        }
        for identity, stage, checksum in [("s0", "baseline", "a"), ("s1", "final", "b")]
    ]
    result = JobResult(
        id="r",
        job_id="j",
        result_s3_key="results/j/s1.zip",
        inline_payload={"checksum": "b" * 64},
        document_metadata={
            "progressive": {"snapshots": snapshots, "current_snapshot_id": "s1"}
        },
    )
    delivery = JobResultDeliveryResolver(storage=storage)
    baseline = delivery.enrich_payload(
        {"event": "job.completed", "snapshot_id": "s0"}, job_result=result
    )
    assert baseline["result_url"] == "https://storage.example/results/j/s0.zip"
    assert baseline["result"]["checksum"] == "a" * 64
    final = delivery.enrich_payload(
        {"event": "job.enrichment.completed", "snapshot_id": "s1"}, job_result=result
    )
    assert final["result_url"] == "https://storage.example/results/j/s1.zip"
