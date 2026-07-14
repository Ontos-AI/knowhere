"""Sequential, auditable batch runner for Codex review package exports."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil

from app.services.codex_export.package_builder import (
    ReviewPackageRequest,
    ReviewPackageResult,
    build_codex_review_package,
)
from app.services.codex_export.validation_corpus import (
    ValidationCorpus,
    ValidationDocument,
)
from app.services.codex_export.validation_report import (
    ValidationReport,
    ValidationRunResult,
    build_validation_report,
)


class PackageAuditError(RuntimeError):
    """Raised when a generated package fails its integrity audit."""


@dataclass(frozen=True)
class ValidationOptions:
    output_root: Path
    mineru_project_path: Path
    repeat: int = 1
    backend: str = "pipeline"
    method: str = "auto"
    dpi: int = 144
    offline: bool = True
    force: bool = False

    def __post_init__(self) -> None:
        if self.repeat < 1:
            raise ValueError("repeat must be positive")
        if self.dpi < 1:
            raise ValueError("dpi must be positive")


class _ResourceMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self.peak_rss_bytes = 0
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        process = psutil.Process()
        while not self._stop.is_set():
            total = 0
            try:
                processes = [process, *process.children(recursive=True)]
            except (psutil.Error, OSError):
                processes = [process]
            for item in processes:
                try:
                    total += item.memory_info().rss
                except (psutil.Error, OSError):
                    continue
            self.peak_rss_bytes = max(self.peak_rss_bytes, total)
            self._stop.wait(0.05)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_artifact(package_root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise PackageAuditError("artifact path is invalid")
    artifact = (package_root / relative).resolve(strict=False)
    root = package_root.resolve(strict=True)
    if not artifact.is_relative_to(root) or not artifact.is_file():
        raise PackageAuditError("artifact path escapes package or does not exist")
    return artifact


def _audit_package(result: ReviewPackageResult) -> tuple[
    dict[str, int], dict[str, int], dict[str, int], int, str
]:
    try:
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageAuditError("package manifest is unreadable") from error
    if manifest.get("status") != "completed":
        raise PackageAuditError("package manifest status is not completed")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise PackageAuditError("package artifact inventory is missing")
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise PackageAuditError("artifact inventory entry is invalid")
        artifact = _safe_artifact(result.package_root, entry.get("path"))
        if artifact.stat().st_size != entry.get("size_bytes"):
            raise PackageAuditError("artifact size does not match inventory")
        if _sha256(artifact) != entry.get("sha256"):
            raise PackageAuditError("artifact hash does not match inventory")

    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not all(
        isinstance(value, int) for value in counts.values()
    ):
        raise PackageAuditError("package counts are invalid")

    fidelity: Counter[str] = Counter()
    table_fingerprints: list[object] = []
    for path in sorted((result.package_root / "tables").glob("*.metadata.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("csv_fidelity", "unknown")
        fidelity[str(value)] += 1
        table_fingerprints.append(
            {"table_id": payload.get("table_id"), "csv_fidelity": value}
        )

    finding_categories: Counter[str] = Counter()
    findings_path = result.package_root / "structured" / "extraction_findings.jsonl"
    if findings_path.is_file():
        for line in findings_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                finding = json.loads(line)
                finding_categories[str(finding.get("category", "unknown"))] += 1

    comparison: dict[str, object] = {
        "tables": table_fingerprints,
        "pages": sorted(path.name for path in (result.package_root / "pages").glob("*.png")),
    }
    for name in ("blocks.jsonl", "document_tree.json"):
        path = result.package_root / "structured" / name
        comparison[name] = _sha256(path) if path.is_file() else None
    fingerprint = hashlib.sha256(
        json.dumps(comparison, sort_keys=True).encode("utf-8")
    ).hexdigest()
    package_bytes = sum(
        path.stat().st_size for path in result.package_root.rglob("*") if path.is_file()
    )
    return (
        dict(counts),
        dict(sorted(fidelity.items())),
        dict(sorted(finding_categories.items())),
        package_bytes,
        fingerprint,
    )


_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"
)


def _sanitize_error(error: Exception, paths: tuple[Path, ...]) -> str:
    message = str(error)
    for path in sorted(paths, key=lambda item: len(str(item)), reverse=True):
        message = message.replace(str(path), "<path>")
    message = _SECRET_PATTERN.sub(r"\1=<redacted>", message)
    return message[:500]


def _request(
    document: ValidationDocument,
    options: ValidationOptions,
    run_number: int,
) -> ReviewPackageRequest:
    return ReviewPackageRequest(
        source_path=document.source_path,
        output_root=(
            options.output_root
            / "packages"
            / document.document_id
            / f"run-{run_number:03d}"
        ),
        mineru_project_path=options.mineru_project_path,
        backend=options.backend,
        method=options.method,
        language=document.language,
        requested_pages=document.pages,
        include_table_pages=True,
        include_image_pages=False,
        dpi=options.dpi,
        offline=options.offline,
        force=options.force,
        keep_work_dir=False,
    )


def run_validation_corpus(
    corpus: ValidationCorpus,
    options: ValidationOptions,
    *,
    package_builder: Callable[[ReviewPackageRequest], ReviewPackageResult] = build_codex_review_package,
) -> ValidationReport:
    """Run every corpus record sequentially and continue after document failures."""

    results: list[ValidationRunResult] = []
    for document in corpus.documents:
        for run_number in range(1, options.repeat + 1):
            monitor = _ResourceMonitor()
            started = time.perf_counter()
            monitor.start()
            actual_status = "failed"
            artifacts_verified = False
            counts: dict[str, int] = {}
            fidelity: dict[str, int] = {}
            categories: dict[str, int] = {}
            package_bytes = 0
            fingerprint: str | None = None
            error_type: str | None = None
            error_message: str | None = None
            try:
                package = package_builder(_request(document, options, run_number))
                counts, fidelity, categories, package_bytes, fingerprint = _audit_package(
                    package
                )
                artifacts_verified = True
                actual_status = "completed"
            except Exception as error:
                error_type = type(error).__name__
                error_message = _sanitize_error(
                    error,
                    (
                        document.source_path,
                        document.source_path.parent,
                        options.output_root,
                        options.mineru_project_path,
                    ),
                )
            finally:
                monitor.stop()
            results.append(
                ValidationRunResult(
                    document_id=document.document_id,
                    filename=document.relative_path.name,
                    tags=document.tags,
                    run_number=run_number,
                    expected_status=document.expected_status,
                    actual_status=actual_status,
                    expectation_matched=actual_status == document.expected_status,
                    reproducible=True,
                    source_sha256=_sha256(document.source_path),
                    source_bytes=document.source_path.stat().st_size,
                    duration_seconds=round(time.perf_counter() - started, 6),
                    peak_rss_bytes=monitor.peak_rss_bytes,
                    package_bytes=package_bytes,
                    counts=counts,
                    table_fidelity=fidelity,
                    finding_categories=categories,
                    artifacts_verified=artifacts_verified,
                    error_type=error_type,
                    error_message=error_message,
                    comparison_fingerprint=fingerprint,
                )
            )

    for document in corpus.documents:
        repeated = [item for item in results if item.document_id == document.document_id]
        signatures = {
            (item.actual_status, item.error_type, item.comparison_fingerprint)
            for item in repeated
        }
        reproducible = len(signatures) == 1
        for item in repeated:
            item.reproducible = reproducible
    return build_validation_report(results)
