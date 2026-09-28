"""Capacity admission uses every batch and leaves failed partial runs red."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event, Timer
from typing import Any

import pytest

from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.capacity_campaign import (  # noqa: E402
    CapacityCampaign,
    evaluate_campaign,
    run_capacity_campaign,
)
from scripts.publication_benchmark.layout import BenchmarkLayout  # noqa: E402
from scripts.publication_benchmark.run_capacity import (  # noqa: E402
    CapacitySample,
    build_argument_parser,
    evaluate_capacity_gate,
)
from scripts.publication_benchmark.retrieval_interference import (  # noqa: E402
    RetrievalProbeSpec,
    run_open_loop_probes,
)


def test_capacity_gate_uses_all_batches_at_the_same_level() -> None:
    baseline = [
        CapacitySample("baseline", level, 0, 0, 0, True, 10, 100, 0.01)
        for level in (1, 10)
        for _ in range(3)
    ]
    candidate = [
        CapacitySample("candidate", level, 0, 0, 0, True, rate, 100, 0.01)
        for level in (1, 10)
        for rate in (5, 5, 10)
    ]
    gate = evaluate_capacity_gate(
        baseline_samples=baseline,
        candidate_samples=candidate,
        knee_concurrency=1,
    )
    assert gate["status"] == "fail"
    assert any("throughput" in failure for failure in gate["failures"])


def test_capacity_cli_accepts_explicit_source_identity() -> None:
    arguments = build_argument_parser().parse_args(
        [
            "--source-volume",
            "knowhere-mac-source-data",
            "--source-container",
            "knowhere-mac-source-pg",
        ]
    )
    assert arguments.source_volume == "knowhere-mac-source-data"
    assert arguments.source_container == "knowhere-mac-source-pg"


def test_failed_retrieval_in_smoke_stays_red() -> None:
    resource = {
        "wal_bytes": 100,
        "table_size_delta_bytes": 100,
        "index_size_delta_bytes": 100,
        "temp_bytes": 0,
        "peak_rss_bytes": 100,
        "cpu_seconds": 1,
        "lock_wait_ms": 0,
        "checkpoint_pressure": 0,
        "connection_usage": 3,
        "free_disk_bytes": 1000,
    }
    samples = [
        {
            "strategy": strategy,
            "concurrency": level,
            "errors": 0,
            "timeouts": 0,
            "deadlocks": 0,
            "converged": True,
            "throughput_per_second": 10,
            "arrival_to_commit_p95_ms": 100,
            "max_sql_seconds": 0.01,
            "probe_covered_publication": True,
            "probe_gate": {"status": "pass"},
            "probe_p95_ms": 10,
            "resource": resource,
        }
        for level in (1, 10)
        for strategy in ("baseline", "candidate")
    ]
    one_level = [sample for sample in samples if sample["concurrency"] == 1]
    partial = evaluate_campaign(
        one_level, knee=1, confirmation_levels=(1,), complete=False
    )
    assert partial["status"] == "partial"
    one_level[1]["probe_gate"] = {"status": "fail"}
    gate = evaluate_campaign(
        one_level, knee=1, confirmation_levels=(1,), complete=False
    )
    assert gate["status"] == "fail"
    assert any("protected retrieval" in failure for failure in gate["failures"])


def test_open_loop_probe_continues_past_minimum_until_publication_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.publication_benchmark import retrieval_interference

    async def fast_probe(**_kwargs: object) -> tuple[str, str, int]:
        return ("result", "revision", 1)

    monkeypatch.setattr(retrieval_interference, "execute_retrieval_probe", fast_probe)
    completed = Event()
    timer = Timer(0.8, completed.set)
    timer.start()
    try:
        samples = asyncio.run(
            run_open_loop_probes(
                database_url="postgresql+psycopg2://postgres@127.0.0.1:65432/unused",
                spec=RetrievalProbeSpec("user", "namespace", "query", ("document",)),
                rate_per_second=3,
                duration_seconds=0.34,
                stop_event=completed,
            )
        )
    finally:
        timer.join()
    assert samples[-1].scheduled_offset_ms >= 1000
    assert all(sample.error is None for sample in samples)


def test_capacity_campaign_revalidates_clone_url_after_every_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.publication_benchmark import capacity_campaign

    @dataclass(frozen=True)
    class Source:
        digest: str = "source-digest"

    @dataclass(frozen=True)
    class Clone:
        source: Source = Source()
        postgres_version: str = "15.17"
        container_name: str = "local-test-container"
        database_identity: dict[str, str] | None = None

        def settings_digest(self) -> str:
            return "settings-digest"

    clone = Clone(
        database_identity={
            "template_id": "sealed",
            "template_content_digest": "template-digest",
        }
    )
    database_urls = iter(
        (
            "postgresql+psycopg2://postgres:old@127.0.0.1:60001/db",
            "postgresql+psycopg2://postgres:first@127.0.0.1:60001/db",
            "postgresql+psycopg2://postgres:second@127.0.0.1:60001/db",
        )
    )
    monkeypatch.setattr(
        capacity_campaign,
        "read_database_url_file",
        lambda *_args, **_kwargs: next(database_urls),
    )
    monkeypatch.setattr(capacity_campaign, "find_listed_clone", lambda **_kwargs: clone)
    monkeypatch.setattr(
        capacity_campaign, "assert_not_source_database", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        capacity_campaign,
        "read_frozen_input",
        lambda *_args, **_kwargs: (
            {"chunks": [{"type": "text", "content": "benchmark fixture"}]},
            type("Manifest", (), {"digest": "sha256:fixture"})(),
        ),
    )
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(
        json.dumps(
            {
                "user_id": "protected-owner-opaque",
                "namespace": "protected-scope-opaque",
                "query": "unique-protected-phrase",
                "protected_document_ids": ["protected-document"],
            }
        ),
        encoding="utf-8",
    )
    used_urls: list[str] = []

    def run_fake_batch(batch: Any) -> dict[str, Any]:
        used_urls.append(batch.database_url)
        return {
            "strategy": batch.strategy,
            "concurrency": 1,
            "errors": 0,
            "timeouts": 0,
            "deadlocks": 0,
            "converged": True,
            "throughput_per_second": 10,
            "arrival_to_commit_p95_ms": 100,
            "max_sql_seconds": 0.01,
            "probe_covered_publication": True,
            "probe_gate": {"status": "pass"},
            "probe_p95_ms": 10,
            "resource": {
                "wal_bytes": 100,
                "table_size_delta_bytes": 100,
                "index_size_delta_bytes": 100,
                "temp_bytes": 0,
                "peak_rss_bytes": 100,
                "cpu_seconds": 1,
                "lock_wait_ms": 0,
                "checkpoint_pressure": 0,
                "connection_usage": 3,
                "free_disk_bytes": 1000,
            },
        }

    run_capacity_campaign(
        CapacityCampaign(
            layout=BenchmarkLayout(tmp_path),
            run_id="clone",
            report_id="report",
            template_id="sealed",
            input_directory=tmp_path,
            probe_spec_path=probe_path,
            owner="sync",
            concurrency_filter=1,
            smoke=True,
        ),
        reset_clone=lambda: clone,
        execute_batch=run_fake_batch,
    )
    assert used_urls == [
        "postgresql+psycopg2://postgres:first@127.0.0.1:60001/db",
        "postgresql+psycopg2://postgres:second@127.0.0.1:60001/db",
    ]
