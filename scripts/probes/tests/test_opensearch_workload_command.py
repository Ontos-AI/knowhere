"""Offline manifest rehearsal is explicitly distinct from staging qualification."""

from __future__ import annotations

import gzip
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest

from test_opensearch_workload import createArrivals, createManifest


def prepareManifest(directory: Path) -> Path:
    manifest: dict[str, object] = createManifest()
    reads, writes = createArrivals()
    for kind, events in (("reads", reads), ("writes", writes)):
        path: Path = directory / (kind + ".json")
        path.write_text(json.dumps(events))
        manifest["source_intervals"][kind]["sha256"] = sha256(path.read_bytes()).hexdigest()
    for case in manifest["read_cases"]:
        path = directory / case["payload_path"]
        path.write_text(json.dumps({"query": "automated component test", "namespace": case["namespace"], "top_k": 10, "use_agentic": False}))
        case["payload_sha256"] = sha256(path.read_bytes()).hexdigest()
    payload: bytes = json.dumps({"chunks": [{"chunk_id": "one", "type": "text", "content": "retained", "path": "Manual"}], "result_s3_key": "retained/job.zip", "checksum": "a" * 64, "zip_size": 1}).encode()
    path = directory / "prepared.json.gz"
    path.write_bytes(gzip.compress(payload))
    prepared: dict[str, object] = manifest["prepared_cases"][0]
    prepared.update({"sha256": sha256(path.read_bytes()).hexdigest(), "compressed_bytes": path.stat().st_size, "uncompressed_bytes": len(payload)})
    result: Path = directory / "manifest.json"
    result.write_text(json.dumps(manifest))
    return result


def runCommand(*arguments: str) -> subprocess.CompletedProcess[str]:
    command: Path = Path(__file__).parents[1] / "run-opensearch-workload.py"
    assert command.is_file(), "Mixed-load command is not implemented"
    return subprocess.run([sys.executable, str(command), *arguments], capture_output=True, text=True, check=False)


def test_default_command_validates_checksums_and_prints_exact_counts_without_services(tmp_path: Path) -> None:
    manifest: Path = prepareManifest(tmp_path)
    result = runCommand("--manifest", str(manifest), "--output", str(tmp_path / "dry-run"))
    assert result.returncode == 0, result.stderr
    plan: dict[str, object] = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["counts"]["reads"] == 70
    assert plan["counts"]["writes"] == 20
    assert plan["qualified"] is False
    assert "automated-inputs-are-not-reviewed-production-arrivals" in plan["missing_acceptance_inputs"]


def test_component_simulation_writes_all_offers_but_cannot_pass_staging_gate(tmp_path: Path) -> None:
    manifest: Path = prepareManifest(tmp_path)
    output: Path = tmp_path / "component-run"
    result = runCommand("--manifest", str(manifest), "--output", str(output), "--simulate")
    assert result.returncode == 0, result.stderr
    report: dict[str, object] = json.loads((output / "report.json").read_text())
    assert report["mode"] == "automated-component-simulation"
    assert report["actual_staging_campaign"] is False
    assert report["qualification"]["qualified"] is False
    offers: list[str] = [line for line in (output / "events.jsonl").read_text().splitlines() if json.loads(line)["event"] == "offered"]
    assert len(offers) == 92
    assert cast(dict[str, int], report["schedule"]["counts"])["rolling_peak_reads_per_second"] == 50


def test_public_quality_checks_asset_reference_and_source_name_independently() -> None:
    path: Path = Path(__file__).parents[1] / "run-opensearch-workload.py"
    specification = importlib.util.spec_from_file_location("mixed_workload_command", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    canonical: dict[str, object] = {"document_id": "document-one", "chunk_id": "chunk-one", "job_result_id": "revision-one", "source_file_name": "Manual",
        "section_path": "Manual / Chapter", "content_sha256": sha256(b"chart").hexdigest(), "file_path": "images/chart.jpg", "asset_path": "/revision-one/images/chart.jpg", "chunk_type": "image", "page_nums": []}
    quality: dict[str, object] = {"namespace": "private", "top_k": 10, "canonical_rows": [canonical], "evidence_groups": [{"postgresql_found": True, "alternatives": [{"document_id": "document-one", "chunk_id": "chunk-one"}]}]}
    response: dict[str, object] = {"namespace": "private", "results": [{"chunk_id": "chunk-one", "job_result_id": "revision-one", "content": "chart", "chunk_type": "image", "file_path": "images/chart.jpg", "asset_url": "https://assets.invalid/wrong-revision/images/chart.jpg",
        "source": {"document_id": "document-one", "job_result_id": "revision-one", "source_file_name": "Wrong manual", "section_path": "Manual / Chapter"}}]}
    preserved, identity = module._verifyQuality(response, quality)
    assert preserved is True
    assert identity is False


def test_telemetry_join_requires_exact_campaign_source_and_unique_event_identity() -> None:
    path: Path = Path(__file__).parents[1] / "run-opensearch-workload.py"
    specification = importlib.util.spec_from_file_location("mixed_workload_telemetry", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    report: dict[str, object] = {"observations": [{"id": "read-one", "quality_passed": False, "status": "failed"}]}
    telemetry: list[dict[str, object]] = [{"id": "read-one", "campaign_id": "old-campaign", "mode": "actual",
        "source_hash": "a" * 64, "lexical_ms": 1, "quality_passed": True}]
    module._joinTelemetry(report, telemetry, campaignId="current", sourceHash="a" * 64)
    assert "lexical_ms" not in report["observations"][0]
    telemetry[0]["campaign_id"] = "current"
    module._joinTelemetry(report, telemetry, campaignId="current", sourceHash="a" * 64)
    assert report["observations"][0]["lexical_ms"] == 1
    assert report["observations"][0]["quality_passed"] is False
    assert report["observations"][0]["status"] == "failed"
    with pytest.raises(ValueError, match="Duplicate"):
        module._joinTelemetry(report, telemetry + telemetry, campaignId="current", sourceHash="a" * 64)


def test_offline_reevaluation_preserves_frozen_campaign_and_raw_report_identity(tmp_path: Path) -> None:
    manifest: Path = prepareManifest(tmp_path)
    original: Path = tmp_path / "original"
    assert runCommand("--manifest", str(manifest), "--output", str(original), "--simulate").returncode == 0
    report: Path = original / "report.json"
    evaluated: Path = tmp_path / "evaluated"
    result = runCommand("--manifest", str(manifest), "--output", str(evaluated), "--evaluate-report", str(report))
    assert result.returncode == 1, result.stderr
    receipt = json.loads((evaluated / "report.json").read_text())
    assert receipt["raw_report_sha256"] == sha256(report.read_bytes()).hexdigest()
    assert receipt["manifest_sha256"] == sha256(manifest.read_bytes()).hexdigest()
    assert receipt["qualification"]["qualified"] is False
