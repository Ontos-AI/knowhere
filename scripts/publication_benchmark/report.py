"""Benchmark report contract: statistics, gates, and redaction.

The report artifacts are ``summary.json``, ``summary.md``, ``resource.json``,
and ``gate-results.json``. Every report is scanned for prohibited production
data before it is written, and the duration gate uses the statistical rule that
was frozen before candidate results existed.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from scripts.publication_benchmark.run_record import PublicationRunRecord

SUMMARY_SCHEMA_VERSION: str = "publication-benchmark-summary/1"
RESOURCE_SCHEMA_VERSION: str = "publication-benchmark-resource/1"
GATE_RESULTS_SCHEMA_VERSION: str = "publication-benchmark-gate-results/1"

DURATION_THRESHOLD_SECONDS: float = 10.0
REQUIRED_COLD_SAMPLES: int = 59
EXPLORATORY_SAMPLE_MINIMUM: int = 20
CONFIDENCE_LEVEL: float = 0.95
DEFAULT_BOOTSTRAP_RESAMPLES: int = 10_000

PROHIBITED_REPORT_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "artifact_path",
        "bind_params",
        "bind_values",
        "chunk_text",
        "content",
        "credentials",
        "database_url",
        "file_path",
        "namespace",
        "password",
        "section_path",
        "source_file_name",
        "sql",
        "sql_text",
        "token_text",
        "tokens",
        "user_id",
    }
)

RESOURCE_FIELDS: tuple[str, ...] = (
    "wal_bytes",
    "table_size_delta_bytes",
    "index_size_delta_bytes",
    "temp_bytes",
    "peak_rss_bytes",
    "cpu_seconds",
    "connection_usage",
    "lock_wait_ms",
    "checkpoint_pressure",
    "free_disk_bytes",
)


class ReportContractError(RuntimeError):
    """Raised when a benchmark report violates its contract."""


@dataclass(frozen=True)
class ResourceSample:
    """Per-publication resource envelope sample."""

    sample_id: str
    values: Mapping[str, float | int | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            **{
                field: self.values.get(field)
                for field in RESOURCE_FIELDS
            },
        }


@dataclass(frozen=True)
class DurationGateResult:
    """Outcome of the frozen one-sided duration rule."""

    status: str
    sample_count: int
    failures: int
    required_samples: int
    threshold_seconds: float
    lower_confidence_bound: float | None
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sample_count": self.sample_count,
            "failures": self.failures,
            "required_samples": self.required_samples,
            "threshold_seconds": self.threshold_seconds,
            "lower_confidence_bound": self.lower_confidence_bound,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class PairedImprovementResult:
    """Paired bootstrap comparison of candidate versus baseline runs."""

    pair_count: int
    mean_improvement_ms: float
    ci_low_ms: float
    ci_high_ms: float
    confidence: float
    resamples: int
    resolved: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_count": self.pair_count,
            "mean_improvement_ms": self.mean_improvement_ms,
            "ci_low_ms": self.ci_low_ms,
            "ci_high_ms": self.ci_high_ms,
            "confidence": self.confidence,
            "resamples": self.resamples,
            "resolved": self.resolved,
            "rationale": self.rationale,
        }


def percentile(values: Sequence[float], fraction: float) -> float:
    """Return the linearly interpolated percentile of a sample."""
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile fraction must be within [0, 1]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def duration_statistics(durations_ms: Sequence[float]) -> dict[str, Any]:
    """Return summary statistics for publication durations in milliseconds."""
    samples = [float(value) for value in durations_ms]
    if not samples:
        return {
            "count": 0,
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
        }
    return {
        "count": len(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "mean_ms": sum(samples) / len(samples),
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
    }


def evaluate_duration_gate(
    durations_seconds: Sequence[float],
    *,
    threshold_seconds: float = DURATION_THRESHOLD_SECONDS,
    required_samples: int = REQUIRED_COLD_SAMPLES,
) -> DurationGateResult:
    """Apply the frozen one-sided zero-failure duration rule."""
    samples = [float(value) for value in durations_seconds]
    failures = sum(1 for value in samples if value >= threshold_seconds)
    if len(samples) < required_samples:
        return DurationGateResult(
            status="insufficient_samples",
            sample_count=len(samples),
            failures=failures,
            required_samples=required_samples,
            threshold_seconds=threshold_seconds,
            lower_confidence_bound=None,
            rationale=(
                f"{len(samples)} samples collected; the frozen rule requires "
                f"{required_samples} independent samples before a duration claim"
            ),
        )
    if failures:
        return DurationGateResult(
            status="fail",
            sample_count=len(samples),
            failures=failures,
            required_samples=required_samples,
            threshold_seconds=threshold_seconds,
            lower_confidence_bound=0.0,
            rationale=(
                f"{failures} of {len(samples)} samples reached "
                f"{threshold_seconds}s; the zero-failure rule is violated"
            ),
        )
    lower_bound = (1.0 - CONFIDENCE_LEVEL) ** (1.0 / len(samples))
    if lower_bound < CONFIDENCE_LEVEL:
        return DurationGateResult(
            status="fail",
            sample_count=len(samples),
            failures=0,
            required_samples=required_samples,
            threshold_seconds=threshold_seconds,
            lower_confidence_bound=lower_bound,
            rationale=(
                f"all {len(samples)} samples are below {threshold_seconds}s but "
                f"the one-sided {CONFIDENCE_LEVEL:.0%} lower bound "
                f"{lower_bound:.4f} does not reach the confidence level"
            ),
        )
    return DurationGateResult(
        status="pass",
        sample_count=len(samples),
        failures=0,
        required_samples=required_samples,
        threshold_seconds=threshold_seconds,
        lower_confidence_bound=lower_bound,
        rationale=(
            f"all {len(samples)} samples are below {threshold_seconds}s, giving a "
            f"one-sided {CONFIDENCE_LEVEL:.0%} lower bound of {lower_bound:.4f}"
        ),
    )


def evaluate_paired_improvement(
    baseline_ms: Sequence[float],
    candidate_ms: Sequence[float],
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = 20260923,
    confidence: float = CONFIDENCE_LEVEL,
) -> PairedImprovementResult:
    """Bootstrap the paired candidate improvement over interleaved samples."""
    if len(baseline_ms) != len(candidate_ms):
        raise ReportContractError(
            "paired bootstrap requires the same number of baseline and "
            "candidate samples"
        )
    if len(baseline_ms) < 2:
        raise ReportContractError(
            "paired bootstrap requires at least two paired samples"
        )
    deltas = [
        float(baseline) - float(candidate)
        for baseline, candidate in zip(baseline_ms, candidate_ms)
    ]
    generator = random.Random(seed)
    sample_size = len(deltas)
    means: list[float] = []
    for _ in range(max(1, resamples)):
        total = 0.0
        for _ in range(sample_size):
            total += deltas[generator.randrange(sample_size)]
        means.append(total / sample_size)
    tail = (1.0 - confidence) / 2.0
    ci_low = percentile(means, tail)
    ci_high = percentile(means, 1.0 - tail)
    resolved = ci_low > 0.0 or ci_high < 0.0
    direction = "improvement" if sum(deltas) >= 0 else "regression"
    return PairedImprovementResult(
        pair_count=sample_size,
        mean_improvement_ms=sum(deltas) / sample_size,
        ci_low_ms=ci_low,
        ci_high_ms=ci_high,
        confidence=confidence,
        resamples=max(1, resamples),
        resolved=resolved,
        rationale=(
            f"bootstrap {confidence:.0%} interval for the paired {direction} "
            f"{'excludes' if resolved else 'includes'} zero; overlapping "
            "intervals require more samples"
        ),
    )


def find_prohibited_keys(document: object, *, path: str = "") -> tuple[str, ...]:
    """Return dotted paths of prohibited report keys."""
    violations: list[str] = []
    if isinstance(document, Mapping):
        for key, value in document.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in PROHIBITED_REPORT_KEYS:
                violations.append(child_path)
            violations.extend(find_prohibited_keys(value, path=child_path))
    elif isinstance(document, (list, tuple)):
        for index, value in enumerate(document):
            violations.extend(find_prohibited_keys(value, path=f"{path}[{index}]"))
    return tuple(violations)


def find_prohibited_values(
    document: object,
    *,
    prohibited_values: Sequence[str],
) -> tuple[str, ...]:
    """Return prohibited values that appear anywhere in a report document."""
    needles = [
        str(value)
        for value in prohibited_values
        if str(value).strip()
    ]
    if not needles:
        return ()
    serialized = (
        document
        if isinstance(document, str)
        else json.dumps(document, sort_keys=True, default=str)
    )
    return tuple(sorted({needle for needle in needles if needle in serialized}))


def assert_report_is_redacted(
    document: object,
    *,
    prohibited_values: Sequence[str] = (),
) -> None:
    """Raise when a report contains prohibited keys or production values."""
    prohibited_keys = find_prohibited_keys(document)
    if prohibited_keys:
        raise ReportContractError(
            f"report contains prohibited keys: {list(prohibited_keys[:10])}"
        )
    leaked_values = find_prohibited_values(document, prohibited_values=prohibited_values)
    if leaked_values:
        raise ReportContractError(
            "report contains prohibited production values: "
            f"{len(leaked_values)} value(s) leaked"
        )


def build_statistics(
    records: Sequence[PublicationRunRecord],
) -> dict[str, Any]:
    """Group duration statistics by owner, mode, and strategy."""
    statistics: dict[str, Any] = {}
    for owner in sorted({record.owner for record in records}):
        for mode in sorted({record.mode for record in records}):
            for strategy in sorted({record.strategy for record in records}):
                samples = [
                    float(record.publication_duration_ms or 0.0)
                    for record in records
                    if record.owner == owner
                    and record.mode == mode
                    and record.strategy == strategy
                    and record.publication_duration_ms is not None
                ]
                if not samples:
                    continue
                statistics[f"{owner}/{mode}/{strategy}"] = duration_statistics(
                    samples
                )
    return statistics


def build_run_summary(
    *,
    run_id: str,
    generated_at: str,
    records: Sequence[PublicationRunRecord],
    terminal_trace_accounting: Mapping[str, Any] | None = None,
    duration_gate: DurationGateResult | None = None,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the machine-readable summary document for one report directory."""
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "sample_count": len(records),
        "statistics": build_statistics(records),
        "duration_gate": (
            duration_gate.to_dict() if duration_gate is not None else None
        ),
        "terminal_trace_accounting": (
            dict(terminal_trace_accounting)
            if terminal_trace_accounting is not None
            else None
        ),
        "records": [record.to_dict() for record in records],
        "notes": list(notes),
    }
    return summary


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    """Render the human-readable summary for one report directory."""
    lines: list[str] = [
        f"# Publication benchmark run {summary.get('run_id')}",
        "",
        f"- generated_at: {summary.get('generated_at')}",
        f"- samples: {summary.get('sample_count')}",
        "",
        "## Duration statistics",
        "",
    ]
    statistics = cast(Mapping[str, Any], summary.get("statistics") or {})
    if statistics:
        lines.append("| scope | count | p50 ms | p95 ms | max ms |")
        lines.append("| --- | --- | --- | --- | --- |")
        for scope in sorted(statistics):
            entry = cast(Mapping[str, Any], statistics[scope])
            lines.append(
                f"| {scope} | {entry.get('count')} | "
                f"{entry.get('p50_ms')} | {entry.get('p95_ms')} | "
                f"{entry.get('max_ms')} |"
            )
    else:
        lines.append("No samples recorded.")

    duration_gate = cast(
        Mapping[str, Any] | None, summary.get("duration_gate") or None
    )
    lines.extend(["", "## Frozen duration gate", ""])
    if duration_gate is None:
        lines.append("Duration gate not evaluated.")
    else:
        lines.append(f"- status: {duration_gate.get('status')}")
        lines.append(f"- samples: {duration_gate.get('sample_count')}")
        lines.append(
            f"- threshold seconds: {duration_gate.get('threshold_seconds')}"
        )
        lines.append(f"- rationale: {duration_gate.get('rationale')}")

    accounting = cast(
        Mapping[str, Any] | None,
        summary.get("terminal_trace_accounting") or None,
    )
    lines.extend(["", "## Terminal trace accounting", ""])
    if accounting is None:
        lines.append("Terminal trace accounting not evaluated.")
    else:
        lines.append(
            f"- accounted: {accounting.get('is_accounted')} "
            f"(attempts={accounting.get('publication_attempts')}, "
            f"terminal_events={accounting.get('terminal_events')})"
        )

    notes = cast(Sequence[str], summary.get("notes") or ())
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def write_report_artifacts(
    *,
    report_directory: Path,
    summary: Mapping[str, Any],
    resource: Mapping[str, Any],
    gate_results: Mapping[str, Any],
    prohibited_values: Sequence[str] = (),
) -> None:
    """Write the four report artifacts after a redaction check."""
    assert_report_is_redacted(summary, prohibited_values=prohibited_values)
    assert_report_is_redacted(resource, prohibited_values=prohibited_values)
    assert_report_is_redacted(gate_results, prohibited_values=prohibited_values)
    markdown = render_summary_markdown(summary)
    assert_report_is_redacted(markdown, prohibited_values=prohibited_values)

    report_directory.mkdir(parents=True, exist_ok=True)
    (report_directory / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (report_directory / "summary.md").write_text(markdown, encoding="utf-8")
    (report_directory / "resource.json").write_text(
        json.dumps(resource, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (report_directory / "gate-results.json").write_text(
        json.dumps(gate_results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_resource_document(
    *,
    run_id: str,
    generated_at: str,
    samples: Sequence[ResourceSample],
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the resource envelope document for one report directory."""
    aggregate: dict[str, Any] = {}
    for field in RESOURCE_FIELDS:
        values = [
            float(value)
            for sample in samples
            for value in [sample.values.get(field)]
            if value is not None
        ]
        aggregate[field] = (
            {
                "count": len(values),
                "max": max(values),
                "mean": sum(values) / len(values),
            }
            if values
            else None
        )
    return {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "samples": [sample.to_dict() for sample in samples],
        "aggregate": aggregate,
        "notes": list(notes),
    }


def build_gate_results_document(
    *,
    run_id: str,
    generated_at: str,
    gates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the gate-results document for one report directory."""
    return {
        "schema_version": GATE_RESULTS_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "gates": [dict(gate) for gate in gates],
    }
