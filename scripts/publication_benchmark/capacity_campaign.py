"""Two-stage, interleaved capacity campaign on one listed clone."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from scripts.publication_benchmark.capacity_execution import (
    CapacityBatch,
    run_capacity_batch,
)
from scripts.publication_benchmark.clone_db import PostgresCloneManager
from scripts.publication_benchmark.clone_state import find_listed_clone
from scripts.publication_benchmark.frozen_input import (
    SPACEX_S1_PRODUCTION_EXPECTATIONS,
    read_frozen_input,
)
from scripts.publication_benchmark.guards import (
    assert_frozen_input_digest,
    assert_not_source_database,
    read_database_url_file,
)
from scripts.publication_benchmark.layout import BenchmarkLayout, validate_run_id
from scripts.publication_benchmark.retrieval_interference import (
    RetrievalProbeSpec,
    digest_json,
)
from scripts.publication_benchmark.report import assert_report_is_redacted
from scripts.publication_benchmark.run_capacity import (
    CAPACITY_LEVELS,
    STAGE_ONE_BATCHES_PER_LEVEL,
    STAGE_TWO_BATCHES_PER_LEVEL,
    evaluate_capacity_gate,
    CapacitySample,
)
from scripts.publication_benchmark.run_record import host_identity


@dataclass(frozen=True)
class CapacityCampaign:
    layout: BenchmarkLayout
    run_id: str
    report_id: str
    template_id: str
    input_directory: Path
    probe_spec_path: Path
    owner: str
    probe_rate: int = 1
    probe_duration_seconds: float = 30.0
    concurrency_filter: int | None = None
    smoke: bool = False
    source_volume: str = "knowhere-prod-restore-data"
    source_container: str = "knowhere-prod-restore-pg"


def select_knee(samples: Sequence[Mapping[str, Any]]) -> int:
    """Take the smallest level reaching 95% of peak median throughput."""
    throughput = {
        level: median(
            float(sample["throughput_per_second"])
            for sample in samples
            if sample["concurrency"] == level
        )
        for level in CAPACITY_LEVELS
        if any(sample["concurrency"] == level for sample in samples)
    }
    if not throughput:
        raise ValueError("capacity knee requires observed batches")
    peak = max(throughput.values())
    return min(level for level, value in throughput.items() if value >= peak * 0.95)


def select_confirmation_levels(samples: Sequence[Mapping[str, Any]]) -> list[int]:
    baseline = [sample for sample in samples if sample["strategy"] == "baseline"]
    candidate = [sample for sample in samples if sample["strategy"] == "candidate"]
    levels = {select_knee(baseline), max(CAPACITY_LEVELS)}
    for level in CAPACITY_LEVELS:
        reference = [sample for sample in baseline if sample["concurrency"] == level]
        proposed = [sample for sample in candidate if sample["concurrency"] == level]
        if not reference or not proposed:
            continue
        reference_rate = median(
            float(sample["throughput_per_second"]) for sample in reference
        )
        proposed_rate = median(
            float(sample["throughput_per_second"]) for sample in proposed
        )
        reference_p95 = median(
            float(sample["arrival_to_commit_p95_ms"]) for sample in reference
        )
        proposed_p95 = median(
            float(sample["arrival_to_commit_p95_ms"]) for sample in proposed
        )
        if proposed_rate < reference_rate * 0.9 or proposed_p95 > reference_p95 * 1.1:
            levels.add(level)
        if any(sample["errors"] or not sample["converged"] for sample in proposed):
            levels.add(level)
    return sorted(levels)


def evaluate_resource_envelope(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    levels: Sequence[int],
) -> dict[str, Any]:
    """Reject material resource growth; absent measurements remain unresolved."""
    failures: list[str] = []
    unresolved: list[str] = []
    metrics = (
        "wal_bytes",
        "table_size_delta_bytes",
        "index_size_delta_bytes",
        "temp_bytes",
        "peak_rss_bytes",
        "cpu_seconds",
        "lock_wait_ms",
        "checkpoint_pressure",
        "connection_usage",
    )
    for level in levels:
        reference = [
            sample["resource"] for sample in baseline if sample["concurrency"] == level
        ]
        proposed = [
            sample["resource"] for sample in candidate if sample["concurrency"] == level
        ]
        if not reference or not proposed:
            failures.append(f"concurrency {level}: missing resource samples")
            continue
        for metric in metrics:
            reference_values = [
                float(value[metric])
                for value in reference
                if value.get(metric) is not None
            ]
            proposed_values = [
                float(value[metric])
                for value in proposed
                if value.get(metric) is not None
            ]
            if len(reference_values) != len(reference) or len(proposed_values) != len(
                proposed
            ):
                unresolved.append(f"concurrency {level}: {metric} unavailable")
                continue
            reference_median = median(reference_values)
            proposed_median = median(proposed_values)
            if reference_median > 0 and proposed_median > reference_median * 1.2:
                failures.append(
                    f"concurrency {level}: {metric} exceeds 120 percent of baseline"
                )
            elif reference_median == 0 and proposed_median > 0:
                unresolved.append(
                    f"concurrency {level}: {metric} appeared from zero baseline"
                )
        if any(
            value.get("free_disk_bytes") is None or value["free_disk_bytes"] <= 0
            for value in proposed
        ):
            failures.append(f"concurrency {level}: free database disk unavailable")
    return {
        "status": "fail" if failures else "review" if unresolved else "pass",
        "failures": failures,
        "unresolved": unresolved,
    }


def evaluate_campaign(
    samples: Sequence[Mapping[str, Any]],
    *,
    knee: int,
    confirmation_levels: Sequence[int],
    complete: bool,
) -> dict[str, Any]:
    baseline = [sample for sample in samples if sample["strategy"] == "baseline"]
    candidate = [sample for sample in samples if sample["strategy"] == "candidate"]
    capacity_gate = evaluate_capacity_gate(
        baseline_samples=[
            CapacitySample(
                **{
                    field: sample[field]
                    for field in CapacitySample.__dataclass_fields__
                }
            )
            for sample in baseline
        ],
        candidate_samples=[
            CapacitySample(
                **{
                    field: sample[field]
                    for field in CapacitySample.__dataclass_fields__
                }
            )
            for sample in candidate
        ],
        knee_concurrency=knee,
        evaluated_levels=None if complete else confirmation_levels,
    )
    failures = list(capacity_gate["failures"])
    for sample in samples:
        if not sample["probe_covered_publication"]:
            failures.append(
                f"concurrency {sample['concurrency']}: probe ended before publication"
            )
        if sample["probe_gate"]["status"] != "pass":
            failures.append(
                f"concurrency {sample['concurrency']}: protected retrieval changed"
            )
    for level in confirmation_levels:
        reference = [sample for sample in baseline if sample["concurrency"] == level]
        proposed = [sample for sample in candidate if sample["concurrency"] == level]
        if not reference or not proposed:
            failures.append(f"concurrency {level}: missing retrieval comparison")
            continue
        reference_p95 = median(float(sample["probe_p95_ms"]) for sample in reference)
        proposed_p95 = median(float(sample["probe_p95_ms"]) for sample in proposed)
        if proposed_p95 > reference_p95 * 1.1:
            failures.append(
                f"concurrency {level}: retrieval p95 exceeds 110 percent of baseline"
            )
    resources = evaluate_resource_envelope(baseline, candidate, confirmation_levels)
    if resources["status"] == "fail":
        failures.extend(resources["failures"])
    return {
        "status": "fail"
        if failures
        else "partial"
        if not complete
        else "review"
        if resources["status"] == "review"
        else "pass",
        "failures": failures,
        "capacity": capacity_gate,
        "resources": resources,
    }


def run_capacity_campaign(
    campaign: CapacityCampaign,
    *,
    reset_clone: Callable[[], Any] | None = None,
    execute_batch: Callable[[CapacityBatch], dict[str, Any]] = run_capacity_batch,
) -> dict[str, Any]:
    """Run full or scoped two-stage samples, resetting before every arm."""
    run_id = validate_run_id(campaign.run_id)
    report_id = validate_run_id(campaign.report_id)
    database_url = read_database_url_file(
        campaign.layout.database_url_path(run_id), purpose="run_capacity"
    )
    clone = find_listed_clone(
        clones_root=campaign.layout.clones_root,
        run_id=run_id,
        database_url=database_url,
    )
    assert_not_source_database(
        database_url, source=clone.source, purpose="run_capacity"
    )
    if not clone.postgres_version.startswith("15."):
        raise ValueError("capacity admission requires PostgreSQL 15 clone")
    payload, manifest = read_frozen_input(
        campaign.input_directory, expectations=SPACEX_S1_PRODUCTION_EXPECTATIONS
    )
    assert_frozen_input_digest(manifest)
    spec = RetrievalProbeSpec.from_path(campaign.probe_spec_path)
    manager = PostgresCloneManager(
        layout=campaign.layout,
        source_volume=campaign.source_volume,
        source_container=campaign.source_container,
    )
    reset = reset_clone or (
        lambda: manager.reset(run_id=run_id, template_id=campaign.template_id)
    )
    levels = (
        (campaign.concurrency_filter,)
        if campaign.concurrency_filter is not None
        else CAPACITY_LEVELS
    )
    samples: list[dict[str, Any]] = []
    database_urls: set[str] = {database_url}
    report_path = campaign.layout.ensure_report_directory(report_id) / "capacity.json"
    template_digest: str | None = None
    settings_digest: str | None = None
    source_digest: str | None = None

    def collect(stage: str, level: int, repetition: int, strategy: str) -> None:
        nonlocal template_digest, settings_digest, source_digest
        fresh = reset()
        fresh_database_url = read_database_url_file(
            campaign.layout.database_url_path(run_id), purpose="run_capacity batch"
        )
        database_urls.add(fresh_database_url)
        find_listed_clone(
            clones_root=campaign.layout.clones_root,
            run_id=run_id,
            database_url=fresh_database_url,
        )
        assert_not_source_database(
            fresh_database_url, source=fresh.source, purpose="run_capacity batch"
        )
        current_template_digest = str(
            fresh.database_identity.get("template_content_digest") or ""
        )
        if not current_template_digest:
            raise ValueError("capacity batch requires a sealed template digest")
        if fresh.database_identity.get("template_id") != campaign.template_id:
            raise ValueError("capacity batch reset used a different sealed template")
        if template_digest is None:
            template_digest = current_template_digest
            settings_digest = fresh.settings_digest()
            source_digest = fresh.source.digest
        elif (
            template_digest != current_template_digest
            or settings_digest != fresh.settings_digest()
            or source_digest != fresh.source.digest
        ):
            raise ValueError("capacity clone identity changed between batches")
        sample = execute_batch(
            CapacityBatch(
                database_url=fresh_database_url,
                run_id=f"{run_id}-{stage}-{level}-{repetition}-{strategy}",
                owner=campaign.owner,
                strategy=strategy,
                concurrency=level,
                chunks=tuple(payload["chunks"]),
                probe_spec=spec,
                probe_rate=campaign.probe_rate,
                probe_duration_seconds=campaign.probe_duration_seconds,
                container_name=fresh.container_name,
            )
        )
        samples.append({**sample, "stage": stage, "repetition": repetition})
        from scripts.publication_benchmark import cli_support

        partial_report = {
            "schema_version": "publication-capacity-campaign/1",
            "status": "running",
            "owner": campaign.owner,
            "run_id": run_id,
            "report_id": report_id,
            "host_id": host_identity(),
            "template_id": campaign.template_id,
            "template_digest": fresh.database_identity.get("template_content_digest"),
            "input_digest": manifest.digest,
            "probe_spec_digest": digest_json(spec.__dict__),
            "postgres_version": fresh.postgres_version,
            "postgres_settings_digest": fresh.settings_digest(),
            "samples": samples,
        }
        assert_report_is_redacted(
            partial_report,
            prohibited_values=(
                spec.user_id,
                spec.namespace,
                spec.query,
                *database_urls,
            ),
        )
        cli_support.write_json(report_path, partial_report)

    for level in levels:
        for repetition in range(1 if campaign.smoke else STAGE_ONE_BATCHES_PER_LEVEL):
            for strategy in (
                ("baseline", "candidate")
                if repetition % 2 == 0
                else ("candidate", "baseline")
            ):
                collect("stage_one", level, repetition, strategy)
    confirmation = (
        select_confirmation_levels(samples)
        if campaign.concurrency_filter is None
        else list(levels)
    )
    knee = select_knee(
        [sample for sample in samples if sample["strategy"] == "baseline"]
    )
    if not campaign.smoke:
        for level in confirmation:
            for repetition in range(STAGE_TWO_BATCHES_PER_LEVEL):
                for strategy in (
                    ("candidate", "baseline")
                    if repetition % 2 == 0
                    else ("baseline", "candidate")
                ):
                    collect("stage_two", level, repetition, strategy)
    gate = evaluate_campaign(
        samples,
        knee=knee,
        confirmation_levels=confirmation,
        complete=(
            campaign.concurrency_filter is None
            and campaign.probe_rate == 1
            and not campaign.smoke
        ),
    )
    from scripts.publication_benchmark import cli_support

    final_report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    final_report["status"] = gate["status"]
    final_report["gate"] = gate
    final_report["knee_concurrency"] = knee
    final_report["confirmation_levels"] = confirmation
    assert_report_is_redacted(
        final_report,
        prohibited_values=(spec.user_id, spec.namespace, spec.query, *database_urls),
    )
    cli_support.write_json(report_path, final_report)
    return {
        "status": gate["status"],
        "report_path": str(report_path),
        "sample_count": len(samples),
        "gate": gate,
    }
