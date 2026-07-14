"""Privacy-safe machine and human readable batch validation reports."""

from __future__ import annotations

import html
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPORT_SCHEMA_VERSION = "codex-validation-report/1.0"


@dataclass
class ValidationRunResult:
    document_id: str
    filename: str
    tags: tuple[str, ...]
    run_number: int
    expected_status: str
    actual_status: str
    expectation_matched: bool
    reproducible: bool
    source_sha256: str
    source_bytes: int
    duration_seconds: float
    peak_rss_bytes: int
    package_bytes: int
    counts: dict[str, int]
    table_fidelity: dict[str, int]
    finding_categories: dict[str, int]
    artifacts_verified: bool
    error_type: str | None = None
    error_message: str | None = None
    comparison_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("comparison_fingerprint", None)
        payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True)
class ValidationReport:
    generated_at: str
    results: tuple[ValidationRunResult, ...]
    summary: dict[str, int]
    schema_version: str = REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "summary": self.summary,
            "results": [result.to_dict() for result in self.results],
        }


def build_validation_report(
    results: list[ValidationRunResult],
) -> ValidationReport:
    total = len(results)
    completed = sum(result.actual_status == "completed" for result in results)
    failed = total - completed
    mismatches = sum(not result.expectation_matched for result in results)
    reproducibility_failures = sum(not result.reproducible for result in results)
    return ValidationReport(
        generated_at=datetime.now(UTC).isoformat(),
        results=tuple(results),
        summary={
            "runs": total,
            "completed": completed,
            "failed": failed,
            "expectation_mismatches": mismatches,
            "reproducibility_failures": reproducibility_failures,
        },
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _render_html(report: ValidationReport) -> str:
    rows: list[str] = []
    for result in report.results:
        values = (
            result.document_id,
            str(result.run_number),
            result.expected_status,
            result.actual_status,
            "yes" if result.expectation_matched else "no",
            "yes" if result.reproducible else "no",
            f"{result.duration_seconds:.3f}",
            str(result.peak_rss_bytes),
            str(result.package_bytes),
            result.error_type or "",
            result.error_message or "",
        )
        rows.append(
            "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>"
        )
    summary = " ".join(
        f"{html.escape(key)}={value}" for key, value in report.summary.items()
    )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Codex export validation</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem}}table{{border-collapse:collapse}}th,td{{border:1px solid #bbb;padding:.35rem;text-align:left}}</style>
</head><body>
<h1>Codex export validation</h1>
<p>{summary}</p>
<table><thead><tr><th>document</th><th>run</th><th>expected</th><th>actual</th><th>matched</th><th>reproducible</th><th>seconds</th><th>peak RSS</th><th>package bytes</th><th>error type</th><th>error</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>
""".format(summary=summary, rows="".join(rows))


def write_validation_reports(
    report: ValidationReport,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Atomically write content-free JSON and standalone escaped HTML reports."""

    json_path = output_dir / "validation-report.json"
    html_path = output_dir / "validation-report.html"
    _atomic_write(
        json_path,
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(html_path, _render_html(report))
    return json_path, html_path
