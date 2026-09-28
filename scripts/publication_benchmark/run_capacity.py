"""Capacity and retrieval-interference campaign for the publication benchmark.

    uv run python scripts/publication_benchmark/run_capacity.py \
      --owner sync \
      --strategy baseline \
      --concurrency 1 \
      --probe-rate 1
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.layout import BenchmarkLayout  # noqa: E402
from scripts.publication_benchmark.report import percentile  # noqa: E402
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PUBLICATION_OWNERS,
)

CAPACITY_LEVELS: tuple[int, ...] = (1, 2, 4, 8, 10)
PROBE_RATES_PER_SECOND: tuple[int, ...] = (1, 3)
PRIMARY_PROBE_RATE: int = 1
BURST_PROBE_RATE: int = 3
STAGE_ONE_BATCHES_PER_LEVEL: int = 5
STAGE_TWO_BATCHES_PER_LEVEL: int = 20
THROUGHPUT_RATIO_FLOOR: float = 0.90
ARRIVAL_P95_RATIO_CEILING: float = 1.10
MAX_SQL_STATEMENT_SECONDS: float = 20.0


class PhaseUnavailableError(RuntimeError):
    """Raised when a command needs a later phase's harness."""


@dataclass(frozen=True)
class CapacitySample:
    """One capacity batch observation."""

    strategy: str
    concurrency: int
    errors: int
    timeouts: int
    deadlocks: int
    converged: bool
    throughput_per_second: float
    arrival_to_commit_p95_ms: float
    max_sql_seconds: float


def build_two_stage_sampling_plan() -> dict[str, Any]:
    """Return the predeclared capacity sampling plan."""
    return {
        "concurrency_levels": list(CAPACITY_LEVELS),
        "probe_rates_per_second": list(PROBE_RATES_PER_SECOND),
        "primary_probe_rate_per_second": PRIMARY_PROBE_RATE,
        "burst_probe_rate_per_second": BURST_PROBE_RATE,
        "stage_one": {
            "batches_per_level": STAGE_ONE_BATCHES_PER_LEVEL,
            "purpose": "locate the saturation knee",
        },
        "stage_two": {
            "batches_per_level": STAGE_TWO_BATCHES_PER_LEVEL,
            "levels": ["knee", "concurrency-10", "possible-regression-levels"],
            "purpose": "confirm the knee and every level showing a regression",
        },
        "interleaving": (
            "baseline and candidate batches are interleaved on the same host "
            "and configuration"
        ),
    }


def evaluate_capacity_gate(
    *,
    baseline_samples: Sequence[CapacitySample],
    candidate_samples: Sequence[CapacitySample],
    knee_concurrency: int,
    max_sql_seconds: float = MAX_SQL_STATEMENT_SECONDS,
    evaluated_levels: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Evaluate the admission gate for capacity and convergence."""
    failures: list[str] = []
    levels = (
        sorted(set(evaluated_levels))
        if evaluated_levels is not None
        else sorted({knee_concurrency, max(CAPACITY_LEVELS)})
    )
    baseline_by_level = _by_concurrency(baseline_samples)
    candidate_by_level = _by_concurrency(candidate_samples)

    for sample in (*baseline_samples, *candidate_samples):
        if sample.concurrency > max(CAPACITY_LEVELS):
            continue
        if sample.errors or sample.timeouts or sample.deadlocks:
            failures.append(
                f"concurrency {sample.concurrency}: "
                f"errors={sample.errors} timeouts={sample.timeouts} "
                f"deadlocks={sample.deadlocks}"
            )
        if not sample.converged:
            failures.append(
                f"concurrency {sample.concurrency}: publications did not converge"
            )
        if sample.max_sql_seconds <= 0 or sample.max_sql_seconds >= max_sql_seconds:
            failures.append(
                f"concurrency {sample.concurrency}: SQL statement "
                f"observation {sample.max_sql_seconds}s is absent or reached {max_sql_seconds}s"
            )

    for level in levels:
        baseline = baseline_by_level.get(level)
        candidate = candidate_by_level.get(level)
        if baseline is None or candidate is None:
            failures.append(f"concurrency {level}: missing comparison samples")
            continue
        throughput_floor = (
            median(sample.throughput_per_second for sample in baseline)
            * THROUGHPUT_RATIO_FLOOR
        )
        candidate_throughput = median(
            sample.throughput_per_second for sample in candidate
        )
        if candidate_throughput < throughput_floor:
            failures.append(
                f"concurrency {level}: throughput "
                f"{candidate_throughput:.3f}/s is below 90 percent "
                f"of baseline ({throughput_floor:.3f}/s)"
            )
        p95_ceiling = (
            median(sample.arrival_to_commit_p95_ms for sample in baseline)
            * ARRIVAL_P95_RATIO_CEILING
        )
        candidate_p95 = median(sample.arrival_to_commit_p95_ms for sample in candidate)
        if candidate_p95 > p95_ceiling:
            failures.append(
                f"concurrency {level}: arrival-to-commit p95 "
                f"{candidate_p95:.1f}ms exceeds 110 percent "
                f"of baseline ({p95_ceiling:.1f}ms)"
            )

    return {
        "status": "pass" if not failures else "fail",
        "evaluated_levels": levels,
        "failures": failures,
    }


def _by_concurrency(
    samples: Sequence[CapacitySample],
) -> dict[int, list[CapacitySample]]:
    grouped: dict[int, list[CapacitySample]] = {}
    for sample in samples:
        grouped.setdefault(sample.concurrency, []).append(sample)
    return grouped


def summarize_arrival_latency(samples: Sequence[CapacitySample]) -> float:
    """Return the p95 arrival-to-commit latency across capacity samples."""
    if not samples:
        return 0.0
    return percentile(
        [sample.arrival_to_commit_p95_ms for sample in samples],
        0.95,
    )


def execute_capacity_batches(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    report_id: str,
    template_id: str,
    input_directory: Path,
    probe_spec_path: Path,
    owner: str,
    concurrency: int | None,
    probe_rate: int,
    probe_duration: float,
    smoke: bool,
    source_volume: str,
    source_container: str,
) -> dict[str, Any]:
    """Run the two-stage campaign against a listed clone and sealed template."""
    from scripts.publication_benchmark.capacity_campaign import (
        CapacityCampaign,
        run_capacity_campaign,
    )

    return run_capacity_campaign(
        CapacityCampaign(
            layout=layout,
            run_id=run_id,
            report_id=report_id,
            template_id=template_id,
            input_directory=input_directory,
            probe_spec_path=probe_spec_path,
            owner=owner,
            concurrency_filter=concurrency,
            probe_rate=probe_rate,
            probe_duration_seconds=probe_duration,
            smoke=smoke,
            source_volume=source_volume,
            source_container=source_container,
        )
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the run_capacity command line parser."""
    parser = argparse.ArgumentParser(
        description="Plan or run Publication capacity batches",
    )
    parser.add_argument("--owner", default="sync", choices=PUBLICATION_OWNERS)
    parser.add_argument(
        "--strategy",
        default="baseline",
        choices=("baseline", "candidate", "both"),
    )
    parser.add_argument("--concurrency", type=int, choices=CAPACITY_LEVELS)
    parser.add_argument("--probe-rate", type=int, choices=PROBE_RATES_PER_SECOND)
    parser.add_argument("--run-id")
    parser.add_argument("--report-id")
    parser.add_argument("--template-id")
    parser.add_argument("--source-volume", default="knowhere-prod-restore-data")
    parser.add_argument("--source-container", default="knowhere-prod-restore-pg")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--probe-spec", type=Path)
    parser.add_argument("--probe-duration", type=float, default=30.0)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run one baseline and one candidate batch; report partial evidence",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute interleaved baseline and candidate batches on a listed clone",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the run_capacity command."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    plan = build_two_stage_sampling_plan()
    if arguments.execute:
        if arguments.strategy != "both":
            build_argument_parser().error("--execute requires --strategy both")
        if not all(
            (
                arguments.run_id,
                arguments.report_id,
                arguments.template_id,
                arguments.input,
                arguments.probe_spec,
            )
        ):
            build_argument_parser().error(
                "--execute requires --run-id, --report-id, --template-id, --input, and --probe-spec"
            )
        if arguments.probe_duration <= 0:
            build_argument_parser().error("--probe-duration must be positive")
        if arguments.smoke and arguments.concurrency is None:
            build_argument_parser().error("--smoke requires --concurrency")
        result = execute_capacity_batches(
            layout=layout,
            run_id=arguments.run_id,
            report_id=arguments.report_id,
            template_id=arguments.template_id,
            input_directory=arguments.input,
            probe_spec_path=arguments.probe_spec,
            owner=arguments.owner,
            concurrency=arguments.concurrency,
            probe_rate=arguments.probe_rate or PRIMARY_PROBE_RATE,
            probe_duration=arguments.probe_duration,
            smoke=arguments.smoke,
            source_volume=arguments.source_volume,
            source_container=arguments.source_container,
        )
        cli_support.print_result({"command": "run_capacity", **result})
        return (
            cli_support.EXIT_OK
            if result["status"] == "pass"
            else cli_support.EXIT_GATE_FAILED
        )
    cli_support.print_result(
        {
            "command": "run_capacity",
            "status": "planned",
            "owner": arguments.owner,
            "strategy": arguments.strategy,
            "concurrency": arguments.concurrency,
            "probe_rate": arguments.probe_rate,
            "admission_gate": {
                "throughput_ratio_floor": THROUGHPUT_RATIO_FLOOR,
                "arrival_p95_ratio_ceiling": ARRIVAL_P95_RATIO_CEILING,
                "max_sql_statement_seconds": MAX_SQL_STATEMENT_SECONDS,
                "zero_errors_required": True,
                "convergence_required": True,
            },
            "plan": plan,
        }
    )
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
