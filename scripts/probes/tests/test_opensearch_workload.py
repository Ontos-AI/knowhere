"""Frozen mixed-load schedules and qualification denominators, without services."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import gzip
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest


def loadWorkload() -> type:
    path: Path = Path(__file__).parents[1] / "opensearch-workload.py"
    assert path.is_file(), "Frozen mixed-workload runner is not implemented"
    specification = importlib.util.spec_from_file_location("native_workload", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.WorkloadSupport)


def createManifest() -> dict[str, object]:
    return {
        "schema_version": "opensearch-mixed-workload-v1", "id": "unit-fixture", "version": "1",
        "provenance": "automated_test", "reviewer": None, "review_receipt_sha256": None, "seed": 42,
        "measured_seconds": 3600, "catchup_seconds": 300, "multiplicity": 10,
        "multiplicity_offsets_ms": [0] * 10,
        "source_intervals": {kind: {"start_utc": "2026-11-01T00:00:00+00:00", "end_utc": "2026-11-01T01:00:00+00:00", "path": kind + ".json", "sha256": "a" * 64,
            "field_definitions": {"offset_ms": "Offset from observed interval start"}, "missing_fields": [], "derivation": None} for kind in ("reads", "writes")},
        "segments": [
            {"id": "measured", "phase": "measured", "source_offset_ms": 0, "duration_ms": 3_600_000, "target_offset_ms": 0},
            {"id": "catchup", "phase": "catchup", "source_offset_ms": 100_000, "duration_ms": 300_000, "target_offset_ms": 3_600_000},
        ],
        "read_cases": [{"id": cohort, "namespace": "private-" + cohort, "cohort": cohort, "namespace_size": "large" if cohort == "warm" else "small", "cache_state": "miss", "request_path": "/v2/retrieval/query", "payload_path": cohort + ".json", "payload_sha256": "b" * 64} for cohort in ("warm", "cold", "capacity_wait")],
        "prepared_cases": [{"id": "prepared-one", "path": "prepared.json.gz", "sha256": "c" * 64, "source_file_name": "prepared.md", "source_revision": "d" * 40, "unit_count": 1, "compressed_bytes": 10, "uncompressed_bytes": 100, "native_bulk_bytes": [100], "representative_largest": False}],
        "cohorts": {cohort: {"planned_offered": count, "minimum_completed_lexical": 1, "preconditioning": "Frozen unit-test setup, not target acceptance", "expected_slot_state": state, "receipt_sha256": "e" * 64} for cohort, count, state in (("warm", 40, "open"), ("cold", 20, "closed_capacity_available"), ("capacity_wait", 10, "closed_capacity_wait"))},
        "expected_counts": {"reads": 70, "writes": 20, "measured_reads": 60, "catchup_reads": 10, "measured_writes": 10, "catchup_writes": 10, "rolling_peak_reads_per_second": 50},
        "bursts": [{"id": "first", "start_ms": 0, "end_ms": 1_000}],
        "overlap_events": [{"id": "snapshot", "kind": "snapshot", "offset_ms": 100_100}, {"id": "maintenance", "kind": "maintenance", "offset_ms": 100_200}],
        "runtime": {"maximum_inflight": 2, "queue_capacity": 100, "request_timeout_seconds": 10, "drain_grace_seconds": 10, "resource_sample_interval_seconds": 10, "maximum_offer_lag_ms": 5, "maximum_start_lag_ms": 5,
            "source_hash": "f" * 64, "configuration_sha256": "1" * 64, "durability_receipt_sha256": "2" * 64, "staging_receipt_sha256": "3" * 64,
            "worker_queue": "knowhere-prepared-unit-fixture", "worker_concurrency": 2, "worker_prefetch": 1, "resource_measurement_contract": {
            **dict.fromkeys(("pg_pool_wait_ms", "pg_transaction_ms", "native_http_pool_wait_ms", "native_response_bytes",
                "native_response_processing_ms", "inline_indexing_ms", "manual_retry_ms", "bulk_ms", "visibility_ms", "native_rejections"), "cumulative-counter"),
            "pg_lock_wait_sampled_lower_bound_ms": "sampled-lower-bound", "gc_collection_time_ms": "collection-time-proxy",
            **dict.fromkeys(("native_connections", "native_task_queue", "cpu_percent", "heap_used_bytes", "disk_free_bytes",
                "open_indexes", "reserved_indexes", "uncertain_indexes", "catalog_indexes", "parse_task_backlog",
                "publication_age_ms", "pg_copy_age_ms"), "gauge"),
        }},
    }


def createArrivals() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    reads: list[dict[str, object]] = [{"id": f"read-{index}", "offset_ms": 0, "case_id": cohort} for index, cohort in enumerate(("warm", "warm", "cold", "cold", "capacity_wait"))]
    reads.append({"id": "ordinary-read", "offset_ms": 100_000, "case_id": "warm"})
    writes: list[dict[str, object]] = [{"id": "ordinary-write", "offset_ms": 100_500, "case_id": "prepared-one", "kind": "create", "job_ids_by_segment": {segment: [f"job-{segment}-{index}" for index in range(10)] for segment in ("measured", "catchup")}}]
    return reads, writes


def test_maps_one_hour_plus_five_minutes_with_tenfold_multiplicity_and_real_rhythm() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    assert plan["counts"] == createManifest()["expected_counts"]
    assert len(plan["events"]) == 92
    arrivals = [event for event in plan["events"] if event["kind"] == "read"]
    assert sum(event["offset_ms"] == 0 for event in arrivals) == 50
    assert not any(1_000 < event["offset_ms"] < 100_000 for event in arrivals)
    assert max(event["offset_ms"] for event in plan["events"]) <= 3_900_000


def test_rejects_wrong_declared_counts_or_missing_write_replica_identity() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    manifest: dict[str, object] = createManifest()
    manifest["expected_counts"]["reads"] = 69
    with pytest.raises(ValueError, match="counts"):
        workload.buildSchedule(manifest, reads, writes)
    writes[0]["job_ids_by_segment"]["catchup"] = ["one-job"]
    with pytest.raises(ValueError, match="replica"):
        workload.buildSchedule(createManifest(), reads, writes)


def test_empty_or_failed_cohorts_and_cancelled_offered_requests_cannot_pass() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    report = workload.evaluateReport(createManifest(), plan, {"completed": False, "cancelled": True, "observations": [], "resource_samples": [], "overlap_results": []})
    assert report["qualified"] is False
    assert "cancelled-or-incomplete-campaign" in report["failures"]
    assert all(cohort["passed"] is False for cohort in report["cohorts"].values())
    assert report["denominators"]["expected_reads"] == 70
    assert report["denominators"]["completed_reads"] == 0


def test_offered_arrivals_continue_when_execution_is_blocked() -> None:
    workload: type = loadWorkload()

    async def run() -> None:
        release: asyncio.Event = asyncio.Event()
        offered: list[str] = []
        now: float = 0

        async def waitUntil(offset: float) -> None:
            nonlocal now
            now = offset
            await asyncio.sleep(0)

        async def execute(event: dict[str, object]) -> dict[str, object]:
            await release.wait()
            return {"id": event["id"], "status": "success"}

        def record(event: dict[str, object]) -> None:
            if event["event"] == "offered":
                offered.append(event["id"])
                if len(offered) == 20:
                    release.set()

        result = await workload.runOpenLoop([{"id": f"request-{index}", "kind": "read", "offset_ms": index} for index in range(20)], execute=execute,
            maximumInflight=1, queueCapacity=2, timeoutSeconds=1, waitUntil=waitUntil, now=lambda: now, record=record)
        assert len(offered) == 20
        assert result["offered"] == 20
        assert result["producer_rejected"] > 0

    asyncio.run(run())


def test_prepared_input_hash_and_classic_only_query_payloads_are_verified(tmp_path: Path) -> None:
    workload: type = loadWorkload()
    payload: Path = tmp_path / "request.json"
    payload.write_text(json.dumps({"query": "unit test", "use_agentic": True}))
    with pytest.raises(ValueError, match="agent"):
        workload.loadReadPayload(payload, sha256(payload.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="checksum"):
        workload.loadReadPayload(payload, "0" * 64)


def test_prepared_completion_uses_owned_lifecycle_boundary_and_observed_readiness(tmp_path: Path) -> None:
    workload: type = loadWorkload()
    chunks: list[dict[str, object]] = [{"chunk_id": "chunk-one", "type": "text", "content": "retained parser output", "path": "Manual / Chapter", "metadata": {"tokens": ["retained"]}}]
    payload: dict[str, object] = {"chunks": chunks, "result_s3_key": "retained/job.zip", "checksum": "a" * 64, "zip_size": 100, "stored_count": 1, "section_summaries": {"Manual / Chapter": "retained summary"}}
    decoded: bytes = json.dumps(payload).encode()
    prepared: Path = tmp_path / "prepared.json.gz"
    prepared.write_bytes(gzip.compress(decoded))
    calls: list[str] = []

    def claim(jobId: str, token: str) -> bool:
        assert jobId == "job-one" and token
        calls.append("claim")
        return True

    def finalize(values: dict[str, object]) -> dict[str, object]:
        calls.append("finalize")
        assert values["chunks"] == chunks
        assert values["parsingOwnerToken"]
        return {"status": "success"}

    def observe() -> dict[str, object]:
        calls.append("observe")
        return {"job_state": "failed", "parsing_status": "completed", "indexing_status": "failed"}

    result = workload.completePrepared(jobId="job-one", path=prepared,
        expectedSha256=sha256(prepared.read_bytes()).hexdigest(), compressedBytes=prepared.stat().st_size,
        uncompressedBytes=len(decoded), claim=claim, finalize=finalize, observe=observe)
    assert calls == ["claim", "finalize", "observe"]
    assert result["status"] == "failed"
    assert result["parsing_status"] == "completed"
    assert result["prepared_parser_boundary_only"] is True


def test_warm_tail_latency_is_checked_on_actual_lexical_samples() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    observations: list[dict[str, object]] = [{"id": event["id"], "status": "success", "quality_passed": True,
        "identity_passed": True, "full_request_ms": 10, "lexical_executions": 1,
        "lexical_ms": 5000 if event.get("cohort") == "warm" else 10,
        "cold_first_ms": 10, "wait_to_discovery_ms": 10, "closed_recovery": event.get("cohort") == "cold",
        "capacity_wait_ms": 1 if event.get("cohort") == "capacity_wait" else 0} for event in plan["events"]]
    report = workload.evaluateReport(createManifest(), plan, {"completed": True, "observations": observations})
    assert report["cohorts"]["warm"]["passed"] is False
    assert report["cohorts"]["warm"]["p95_ms"] == 5000
    assert report["cohorts"]["cold"]["passed"] is True
    assert report["cohorts"]["capacity_wait"]["passed"] is True


def test_failed_indexing_jobs_remain_in_frozen_burst_catchup_denominator() -> None:
    workload: type = loadWorkload()
    manifest: dict[str, object] = createManifest()
    manifest["bursts"] = [{"id": "writes", "start_ms": 100_000, "end_ms": 100_600}]
    reads, writes = createArrivals()
    plan = workload.buildSchedule(manifest, reads, writes)
    observations: list[dict[str, object]] = [{"id": event["id"], "status": "failed", "job_state": "failed",
        "admitted_offset_ms": event["offset_ms"], "search_ready_offset_ms": None} for event in plan["events"] if event["kind"] == "write"]
    report = workload.evaluateReport(manifest, plan, {"completed": True, "observations": observations})
    assert len(report["catchup"][0]["frozen_eligible_ids"]) == 10
    assert report["catchup"][0]["passed"] is False
    assert report["catchup"][0]["exclusions"] == []


def test_only_verified_domain_exclusions_are_separate_from_normal_failures() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    write = next(event for event in plan["events"] if event["kind"] == "write")
    row: dict[str, object] = {"id": write["id"], "status": "failed", "exclusion_reason": "superseded", "exclusion_verified": True}
    result = workload.evaluateReport(createManifest(), plan, {"completed": True, "observations": [row]})
    assert result["denominators"]["verified_domain_exclusions"] == 1
    assert result["denominators"]["failed_or_cancelled"] == 1
    assert "normal-load-failed-timeout-rejected-or-cancelled-work" not in result["failures"]
    row["exclusion_verified"] = False
    result = workload.evaluateReport(createManifest(), plan, {"completed": True, "observations": [row]})
    assert "normal-load-failed-timeout-rejected-or-cancelled-work" in result["failures"]


def test_missing_nullable_telemetry_reports_failure_without_abandoning_other_cohorts() -> None:
    workload: type = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    event = next(event for event in plan["events"] if event["kind"] == "read")
    result = workload.evaluateReport(createManifest(), plan, {"completed": True, "observations": [{"id": event["id"], "status": "success", "capacity_wait_ms": None, "lexical_executions": None, "path_channel_calls": None}]})
    assert result["qualified"] is False
    assert len(result["cohorts"]) == 3
    assert "missing-or-invalid-native-execution-observation" in result["failures"]


def test_preboundary_verified_exclusions_remain_visible_without_admission_timestamp() -> None:
    workload: type = loadWorkload()
    manifest: dict[str, object] = createManifest()
    manifest["bursts"] = [{"id": "writes", "start_ms": 100_000, "end_ms": 100_600}]
    reads, writes = createArrivals()
    schedule = workload.buildSchedule(manifest, reads, writes)
    expected = [event for event in schedule["events"] if event["kind"] == "write" and event["offset_ms"] <= 100_600]
    observations = [{"id": event["id"], "job_id": event["job_id"], "status": "excluded", "exclusion_verified": True,
        "exclusion_reason": "superseded"} for event in expected]
    result = workload.evaluateReport(manifest, schedule, {"completed": True, "observations": observations})
    excluded = result["catchup"][0]["exclusions"]
    assert {row["id"] for row in excluded} == {event["id"] for event in expected}
    assert {row["job_id"] for row in excluded} == {event["job_id"] for event in expected}
    assert result["catchup"][0]["unclassified_offered_write_ids"] == []


def test_named_cold_cohort_does_not_count_a_warm_sample() -> None:
    workload = loadWorkload()
    reads, writes = createArrivals()
    plan = workload.buildSchedule(createManifest(), reads, writes)
    row = next(event for event in plan["events"] if event.get("cohort") == "cold")
    result = workload.evaluateReport(createManifest(), plan, {"observations": [{
        "id": row["id"], "status": "success", "quality_passed": True, "identity_passed": True,
        "lexical_executions": 1, "cold_first_ms": 10, "capacity_wait_ms": 0, "closed_recovery": False,
    }]})
    assert result["cohorts"]["cold"]["completed_lexical_samples"] == 0


def test_write_admission_straddling_boundary_remains_unclassified() -> None:
    workload = loadWorkload()
    manifest = createManifest()
    manifest["bursts"] = [{"id": "writes", "start_ms": 100_000, "end_ms": 100_600}]
    reads, writes = createArrivals()
    plan = workload.buildSchedule(manifest, reads, writes)
    row = next(event for event in plan["events"] if event["kind"] == "write")
    result = workload.evaluateReport(manifest, plan, {"observations": [{
        "id": row["id"], "status": "success", "job_state": "done",
        "admission_started_offset_ms": 100_599, "admitted_offset_ms": 100_601, "search_ready_offset_ms": 100_605,
    }]})
    assert row["id"] in result["catchup"][0]["unclassified_offered_write_ids"]
