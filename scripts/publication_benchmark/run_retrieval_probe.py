"""Run read-only retrieval interference probes against one listed clone.

    uv run python scripts/publication_benchmark/run_retrieval_probe.py \
      --run-id capacity-batch-001 --probe-spec /tmp/retrieval-probe.json \
      --rate 1 --duration 30 --report-id capacity-probe-001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.clone_state import find_listed_clone  # noqa: E402
from scripts.publication_benchmark.guards import (  # noqa: E402
    assert_not_source_database,
    read_database_url_file,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    apply_publication_environment,
    database_url_for_owner,
)
from scripts.publication_benchmark.report import percentile  # noqa: E402
from scripts.publication_benchmark.run_record import host_identity  # noqa: E402
from scripts.publication_benchmark.retrieval_interference import (  # noqa: E402
    RetrievalProbeSpec,
    digest_json,
    evaluate_interference_pair,
    evaluate_probe_samples,
    run_open_loop_probes,
)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe retrieval during capacity load")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--probe-spec", type=Path, required=True)
    parser.add_argument("--owner", required=True, choices=("sync", "async"))
    parser.add_argument("--strategy", required=True, choices=("baseline", "candidate"))
    parser.add_argument("--concurrency", required=True, type=int, choices=(1, 2, 4, 8, 10))
    parser.add_argument("--rate", type=int, choices=(1, 3), default=1)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--expected-report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    run_id = validate_run_id(arguments.run_id)
    report_id = validate_run_id(arguments.report_id)
    database_url = read_database_url_file(
        layout.database_url_path(run_id), purpose="run_retrieval_probe"
    )
    clone_record = find_listed_clone(
        clones_root=layout.clones_root,
        run_id=run_id,
        database_url=database_url,
    )
    assert_not_source_database(
        database_url, source=clone_record.source, purpose="run_retrieval_probe"
    )
    apply_publication_environment(
        database_url=database_url_for_owner(database_url, owner="async"),
        run_id=run_id,
        strategy=arguments.strategy,
    )
    spec = RetrievalProbeSpec.from_path(arguments.probe_spec)
    spec_digest = digest_json(
        {
            "user_id": spec.user_id,
            "namespace": spec.namespace,
            "query": spec.query,
            "protected_document_ids": spec.protected_document_ids,
            "top_k": spec.top_k,
        }
    )
    expected: dict[str, Any] = {}
    if arguments.expected_report:
        expected = json.loads(arguments.expected_report.read_text(encoding="utf-8"))
        if expected.get("spec_digest") != spec_digest:
            raise ValueError("expected report uses a different frozen probe spec")
        if expected.get("gate", {}).get("status") != "pass":
            raise ValueError("expected baseline report did not pass")
    samples = asyncio.run(
        run_open_loop_probes(
            database_url=database_url,
            spec=spec,
            rate_per_second=arguments.rate,
            duration_seconds=arguments.duration,
        )
    )
    gate = evaluate_probe_samples(
        samples,
        expected_result_digest=expected.get("gate", {}).get("result_digest"),
        expected_revision_digest=expected.get("gate", {}).get("revision_digest"),
    )
    report: dict[str, Any] = {
        "schema_version": "publication-retrieval-interference/1",
        "run_id": run_id,
        "report_id": report_id,
        "clone_id": clone_record.clone_id,
        "host_id": host_identity(),
        "postgres_settings_digest": clone_record.settings_digest(),
        "clone_source_digest": clone_record.source.digest,
        "owner": arguments.owner,
        "strategy": arguments.strategy,
        "concurrency": arguments.concurrency,
        "template_digest": clone_record.database_identity.get("template_content_digest"),
        "spec_digest": spec_digest,
        "rate_per_second": arguments.rate,
        "duration_seconds": arguments.duration,
        "sample_count": len(samples),
        "p95_duration_ms": percentile([sample.duration_ms for sample in samples], 0.95),
        "max_arrival_lag_ms": max((sample.arrival_lag_ms for sample in samples), default=0),
        "gate": gate,
        "samples": [sample.to_dict() for sample in samples],
    }
    if expected:
        pair_gate = evaluate_interference_pair(
            baseline_report=expected, candidate_report=report
        )
        report["pair_gate"] = pair_gate
        if pair_gate["status"] != "pass":
            report["gate"] = {
                **gate,
                "status": "fail",
                "failures": [*gate["failures"], *pair_gate["failures"]],
            }
    report_path = layout.ensure_report_directory(report_id) / "retrieval-interference.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    cli_support.print_result(
        {
            "command": "run_retrieval_probe",
            "status": report["gate"]["status"],
            "report_path": str(report_path),
            "sample_count": len(samples),
            "p95_duration_ms": report["p95_duration_ms"],
            "gate": report["gate"],
        }
    )
    return cli_support.EXIT_OK if report["gate"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
