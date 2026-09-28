"""Contract tests for split formal publication campaign aggregation."""

# ruff: noqa: E402

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.formal_campaign_report import (  # noqa: E402
    build_formal_campaign_report,
)


def _write_sample(
    root: Path,
    *,
    round_number: int,
    owner: str,
    strategy: str,
    duration_ms: float,
    duplicate_trace: bool = False,
    mismatched_template: bool = False,
) -> None:
    run_id = f"formal59-compact-{round_number:02d}-{owner}-{strategy}"
    sample_id = f"{run_id}-sample"
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "sample_id": sample_id,
        "strategy": strategy,
        "owner": owner,
        "mode": "cold",
        "code_commit": "dc37bace0d96d7fcacf70917e117256b1104b26",
        "source_content_digest": "sha256:" + "1" * 64,
        "dependency_lock_digest": "sha256:" + "2" * 64,
        "postgres_profile": "production",
        "postgres_version": "15.19",
        "postgres_settings_digest": "sha256:" + "3" * 64,
        "redis_namespace": f"publication-benchmark:{run_id}",
        "input_digest": "sha256:" + "4" * 64,
        "clone_id": run_id,
        "clone_source_digest": "sha256:" + "5" * 64,
        "host_id": "host-test",
        "started_at": "2026-09-25T00:00:00Z",
        "ended_at": "2026-09-25T00:00:01Z",
        "outcome": "committed",
        "publication_duration_ms": duration_ms,
        "publication_attempt_ref": f"attempt-{sample_id}",
        "template_content_digest": "sha256:" + "6" * 64,
        "trace_enabled": True,
        "sql_observation": {"write_row_count_complete": True},
        "counts": {
            "persisted_document_chunks": 922,
            "persisted_map_units": 830,
            "persisted_token_rows": 88551,
            "submitted_chunks": 922,
            "submitted_image_chunks": 99,
            "submitted_table_chunks": 153,
            "submitted_text_chunks": 670,
        },
    }
    (directory / "publication.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    setting_digest = "sha256:" + sha256(b"setting=value").hexdigest()
    record["postgres_settings_digest"] = setting_digest
    (directory / "publication.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    template_digest = "sha256:" + ("7" if mismatched_template else "6") * 64
    clone = {
        "clone_id": run_id,
        "run_id": run_id,
        "created_at": "2026-09-25T00:00:00Z",
        "profile": "production",
        "postgres_image": "postgres:15-alpine",
        "postgres_version": "15.19",
        "schema_revision": "2b3c4d5e6f70",
        "source": {
            "volume_name": "source-volume",
            "digest": "sha256:" + "8" * 64,
            "container_name": "source-container",
            "database_name": "source",
            "database_url_host": "127.0.0.1",
            "database_url_port": 5432,
        },
        "data_volume": f"clone-volume-{run_id}",
        "container_name": f"clone-container-{run_id}",
        "port": 55432,
        "database_name": "benchmark",
        "database_url_digest": "sha256:" + "9" * 64,
        "database_identity": {
            "system_identifier": "system",
            "database_name": "benchmark",
            "database_oid": 1,
            "size_bytes": 1,
            "template_content_digest": template_digest,
        },
        "server_settings": {"setting": "value"},
        "index_inventory": [{"index": "document_map_unit_tokens_pkey"}],
        "deviations": [],
        "state": "sampled",
    }
    (directory / "clone.json").write_text(json.dumps(clone), encoding="utf-8")
    trace = {"attempt_ref": record["publication_attempt_ref"], "outcome": "success"}
    trace_lines = [trace, trace] if duplicate_trace else [trace]
    (directory / "publication-traces.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in trace_lines), encoding="utf-8"
    )
    (directory / "trace-accounting.json").write_text(
        json.dumps(
            {"terminal_event_attempt_refs": [record["publication_attempt_ref"]]}
        ),
        encoding="utf-8",
    )


def test_split_formal_campaign_requires_paired_samples_and_marks_unresolved_gates(
    tmp_path: Path,
) -> None:
    clones_root = tmp_path / "clones"
    for round_number in range(1, 60):
        for owner in ("sync", "async"):
            _write_sample(
                clones_root,
                round_number=round_number,
                owner=owner,
                strategy="baseline",
                duration_ms=20_000,
            )
            _write_sample(
                clones_root,
                round_number=round_number,
                owner=owner,
                strategy="candidate",
                duration_ms=6_000,
            )

    report = build_formal_campaign_report(
        clones_root=clones_root,
        repository_root=Path.cwd(),
        generated_at="2026-09-26T00:00:00Z",
    )

    assert report["sample_count"] == 236
    assert report["structure"]["status"] == "pass"
    assert report["duration"]["status"] == "pass"
    assert report["paired_improvement"]["status"] == "pass"
    assert report["trace_accounting"]["status"] == "pass"
    assert report["state_parity"]["status"] == "insufficient_evidence"
    assert report["capacity"]["status"] == "insufficient_evidence"
    assert report["status"] == "insufficient_evidence"
    serialized = json.dumps(report, sort_keys=True)
    assert "scope_ref" not in serialized
    assert "publication.jsonl" not in serialized


def test_split_formal_campaign_rejects_duplicate_trace_and_clone_template_mismatch(
    tmp_path: Path,
) -> None:
    clones_root = tmp_path / "clones"
    for round_number in range(1, 60):
        for owner in ("sync", "async"):
            for strategy in ("baseline", "candidate"):
                _write_sample(
                    clones_root,
                    round_number=round_number,
                    owner=owner,
                    strategy=strategy,
                    duration_ms=6_000,
                )
    _write_sample(
        clones_root,
        round_number=1,
        owner="sync",
        strategy="baseline",
        duration_ms=6_000,
        duplicate_trace=True,
        mismatched_template=True,
    )

    report = build_formal_campaign_report(
        clones_root=clones_root,
        repository_root=Path.cwd(),
        generated_at="2026-09-26T00:00:00Z",
    )

    assert report["structure"]["status"] == "fail"
    assert report["trace_accounting"]["status"] == "fail"
    assert report["status"] == "fail"
