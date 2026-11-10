"""Delayed initial webhooks must continue to identify their baseline artifact."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from shared.models.database.job_result import JobResult
from shared.services.jobs.result_delivery import JobResultDeliveryResolver
from shared.services.webhook import payload_enrichment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"event": "job.parsed"}, {"event": "job.failed", "failure_stage": "indexing"}],
)
async def test_webhook_enricher_loads_result_for_parse_milestones(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, str]
) -> None:
    job_result = JobResult(id="r", job_id="j")
    job = Mock(job_result=job_result)
    result = Mock()
    result.scalar_one_or_none.return_value = job
    db = Mock(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def database_context() -> AsyncIterator[Mock]:
        yield db

    monkeypatch.setattr(payload_enrichment, "get_db_context", database_context)
    resolver = Mock(spec=JobResultDeliveryResolver)
    resolver.supports_payload.side_effect = JobResultDeliveryResolver.supports_payload
    resolver.enrich_payload.return_value = {**payload, "result_url": "https://example.invalid"}
    event = Mock(payload=payload, job_id="j")

    assert await payload_enrichment.WebhookPayloadEnricher(resolver).enrich(event) == {
        **payload, "result_url": "https://example.invalid"
    }
    resolver.enrich_payload.assert_called_once_with(payload, job_result=job_result)


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
