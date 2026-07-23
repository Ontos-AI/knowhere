"""Atomic builder for portable Codex document review packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

from app.services.codex_export.block_normalizer import normalize_content_list_v2
from app.services.codex_export.docx_render import (
    probe_libreoffice_version,
    render_docx_to_normalized_pdf,
)
from app.services.codex_export.instructions import build_codex_review_instructions
from app.services.codex_export.jsonl import write_blocks_jsonl, write_findings_jsonl
from app.services.codex_export.page_selection import (
    RenderedPage,
    render_review_pages,
    resolve_selected_pages,
)
from app.services.codex_export.schema import ExtractionFinding, deterministic_id
from app.services.codex_export.table_exporter import export_tables
from app.services.codex_export.tree_builder import build_document_tree
from app.services.document_parser.providers.mineru.artifact_contract import (
    MinerUArtifactBundle,
)
from app.services.document_parser.providers.mineru.local_process import (
    LocalMinerURequest,
    LocalMinerURunner,
)
from shared.core.exceptions.domain_exceptions import LibreOfficeServiceException


PACKAGE_SCHEMA_VERSION = "codex-review-package/1.0"
_SUPPORTED_SUFFIXES = {".pdf", ".docx"}


class ReviewPackageError(RuntimeError):
    """Raised when a review package cannot be safely built."""


@dataclass(frozen=True)
class ReviewPackageRequest:
    source_path: Path
    output_root: Path
    mineru_project_path: Path
    backend: str
    method: str
    language: str
    requested_pages: tuple[int, ...]
    include_table_pages: bool
    include_image_pages: bool
    dpi: int
    offline: bool
    force: bool
    keep_work_dir: bool


@dataclass(frozen=True)
class ReviewPackageResult:
    package_root: Path
    manifest_path: Path
    document_id: str
    block_count: int
    table_count: int
    page_count: int
    work_dir: Path | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _validate_request(
    request: ReviewPackageRequest,
) -> tuple[Path, Path, Path, str]:
    source = request.source_path.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ReviewPackageError("Input must be one existing local PDF or DOCX file.")
    if any(page < 1 for page in request.requested_pages):
        raise ReviewPackageError("Requested pages must be positive one-based numbers.")
    if not 72 <= request.dpi <= 300:
        raise ReviewPackageError("DPI must be between 72 and 300.")
    project = request.mineru_project_path.expanduser().resolve()
    if not project.is_dir():
        raise ReviewPackageError("MinerU project path must be an existing directory.")
    output = request.output_root.expanduser().resolve()
    if output.exists() and not request.force:
        raise ReviewPackageError(f"Output already exists: {output.name}")
    if output == source or output in source.parents:
        raise ReviewPackageError("Output path must not contain or replace the source file.")
    return source, output, project, source.suffix.lower()


def _copy_mineru_artifacts(
    bundle: MinerUArtifactBundle,
    package_root: Path,
) -> None:
    raw_root = package_root / "raw" / "mineru"
    raw_root.mkdir(parents=True, exist_ok=True)
    for source_path in (
        bundle.middle_json_path,
        bundle.content_list_path,
        bundle.content_list_v2_path,
        bundle.manifest_path,
    ):
        shutil.copy2(source_path, raw_root / source_path.name)
    raw_images = raw_root / "images"
    assets = package_root / "assets"
    if bundle.images_dir.is_dir():
        shutil.copytree(bundle.images_dir, raw_images, dirs_exist_ok=True)
        shutil.copytree(bundle.images_dir, assets, dirs_exist_ok=True)


def _copy_mineru_log(work_root: Path, package_root: Path) -> None:
    source_log = work_root / "logs" / "mineru.log"
    if source_log.is_file():
        destination = package_root / "logs" / "mineru.log"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_log, destination)


def _pdf_page_count(path: Path) -> int:
    document = pymupdf.open(path)
    try:
        return document.page_count
    finally:
        document.close()


def _git_commit() -> str | None:
    repository_root = Path(__file__).resolve().parents[5]
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _docx_finding(
    *,
    document_id: str,
    severity: str,
    message: str,
) -> ExtractionFinding:
    return ExtractionFinding(
        finding_id=deterministic_id(
            "ext", document_id, "docx_rendering", severity, message
        ),
        severity=severity,
        category="docx_rendering",
        message=message,
        document_id=document_id,
        block_id=None,
        page_number=None,
        native_verification_required=True,
    )


def _artifact_inventory(package_root: Path) -> list[dict[str, Any]]:
    manifest_path = package_root / "metadata" / "manifest.json"
    inventory = []
    for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
        if path == manifest_path:
            continue
        relative = path.relative_to(package_root).as_posix()
        inventory.append(
            {
                "path": relative,
                "category": relative.split("/", 1)[0],
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return inventory


def _finalize_directory(
    temporary_package: Path,
    final_package: Path,
    *,
    force: bool,
) -> None:
    backup: Path | None = None
    if final_package.exists():
        if not force:
            raise ReviewPackageError(f"Output already exists: {final_package.name}")
        backup = final_package.parent / (
            f".{final_package.name}.backup-{uuid.uuid4().hex}"
        )
        final_package.replace(backup)
    try:
        temporary_package.replace(final_package)
    except Exception:
        if backup is not None and backup.exists() and not final_package.exists():
            backup.replace(final_package)
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup)


def _preserve_failed_work(
    *,
    temporary_package: Path,
    work_root: Path,
    final_package: Path,
) -> Path:
    failed_root = final_package.parent / (
        f".failed-{final_package.name}-{uuid.uuid4().hex}"
    )
    failed_root.mkdir(parents=True)
    if temporary_package.exists():
        temporary_package.replace(failed_root / "package")
    if work_root.exists():
        work_root.replace(failed_root / "work")
    return failed_root


def build_codex_review_package(
    request: ReviewPackageRequest,
) -> ReviewPackageResult:
    """Build and atomically publish one standalone Codex review package."""
    source, final_package, mineru_project, suffix = _validate_request(request)
    final_package.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    temporary_package = final_package.parent / (
        f".{final_package.name}.building-{run_id}"
    )
    work_root = final_package.parent / f".{final_package.name}.work-{run_id}"
    temporary_package.mkdir()
    work_root.mkdir()

    source_hash = _sha256_file(source)
    document_id = f"doc_{source_hash[:16]}"
    blocks = []
    table_results = []
    rendered_pages: list[RenderedPage] = []
    retained_work: Path | None = None
    try:
        uv_executable = os.environ.get("MINERU_LOCAL_UV_EXECUTABLE", "uv")
        timeout_seconds = float(
            os.environ.get("MINERU_LOCAL_TIMEOUT_SECONDS", "1800")
        )
        max_log_chars = int(os.environ.get("MINERU_LOCAL_MAX_LOG_CHARS", "8000"))
        runner = LocalMinerURunner(
            project_path=mineru_project,
            uv_executable=uv_executable,
            timeout_seconds=timeout_seconds,
            max_log_chars=max_log_chars,
        )
        bundle = runner.run(
            LocalMinerURequest(
                source_path=source,
                output_root=work_root / "mineru",
                backend=request.backend,
                method=request.method,
                language=request.language,
                offline=request.offline,
            )
        )

        native_path = temporary_package / "native" / f"source{suffix}"
        native_path.parent.mkdir(parents=True)
        shutil.copy2(source, native_path)
        derivative_path = temporary_package / "derivatives" / "document.md"
        derivative_path.parent.mkdir(parents=True)
        shutil.copy2(bundle.markdown_path, derivative_path)
        _copy_mineru_artifacts(bundle, temporary_package)
        _copy_mineru_log(work_root, temporary_package)

        blocks = normalize_content_list_v2(
            artifact_bundle=bundle,
            document_id=document_id,
        )
        tree = build_document_tree(blocks)
        table_results = export_tables(
            blocks=blocks,
            package_root=temporary_package,
        )
        findings = [
            finding
            for block in blocks
            for finding in block.findings
        ]
        findings.extend(tree.findings)
        for table_result in table_results:
            findings.extend(table_result.findings)

        conversion: dict[str, Any] = {
            "source_format": suffix.lstrip("."),
            "dpi": request.dpi,
            "requested_pages": sorted(set(request.requested_pages)),
        }
        rendering_pdf: Path | None = native_path if suffix == ".pdf" else None
        if suffix == ".docx":
            conversion.update(
                {
                    "converter": "LibreOffice",
                    "converter_version": probe_libreoffice_version(),
                    "normalized_pdf_mapping_status": "unmapped",
                    "page_number_semantics": "normalized_pdf",
                    "font_environment_limitation": (
                        "Pagination may vary by LibreOffice version, fonts, platform, "
                        "and printer settings."
                    ),
                }
            )
            try:
                rendering_pdf = render_docx_to_normalized_pdf(
                    docx_path=source,
                    output_dir=temporary_package / "normalized",
                )
                conversion["converted_at"] = _utc_now()
            except LibreOfficeServiceException as error:
                message = (
                    "LibreOffice normalized-PDF rendering was unavailable; structured "
                    "artifacts remain available and logical pages remain unmapped."
                )
                findings.append(
                    _docx_finding(
                        document_id=document_id,
                        severity="error",
                        message=message,
                    )
                )
                conversion["conversion_error"] = type(error).__name__
                if request.requested_pages:
                    raise ReviewPackageError(
                        "DOCX page rendering was requested but LibreOffice is unavailable."
                    ) from error
            findings.append(
                _docx_finding(
                    document_id=document_id,
                    severity="warning",
                    message=(
                        "DOCX normalized-PDF pagination may vary by converter version, "
                        "fonts, platform, and printer settings; MinerU logical pages are "
                        "not mapped to rendered pages."
                    ),
                )
            )
        else:
            conversion["page_number_semantics"] = "native_pdf"

        selected_pages: list[int] = []
        if rendering_pdf is not None:
            pdf_page_count = _pdf_page_count(rendering_pdf)
            selected_pages = resolve_selected_pages(
                requested_pages=request.requested_pages,
                blocks=blocks,
                include_table_pages=request.include_table_pages,
                include_image_pages=request.include_image_pages,
                page_count=pdf_page_count,
            )
            rendered_pages = render_review_pages(
                pdf_path=rendering_pdf,
                pages=selected_pages,
                output_dir=temporary_package / "pages",
                dpi=request.dpi,
                page_number_semantics=(
                    "normalized_pdf" if suffix == ".docx" else "native_pdf"
                ),
            )
        conversion["selected_pages"] = selected_pages

        structured = temporary_package / "structured"
        write_blocks_jsonl(blocks, structured / "blocks.jsonl")
        _write_json(structured / "document_tree.json", tree.to_dict())
        write_findings_jsonl(findings, structured / "extraction_findings.jsonl")
        (temporary_package / "CODEX_REVIEW_INSTRUCTIONS.md").write_text(
            build_codex_review_instructions(document_id),
            encoding="utf-8",
        )
        (temporary_package / ".gitignore").write_text(
            "*\n!.gitignore\n", encoding="utf-8", newline="\n"
        )

        warnings = list(bundle.manifest.warnings)
        for result in table_results:
            warnings.extend(result.warnings)
        limitations = [
            "Extracted text, structure, tables, images, and page renders are derivatives.",
            "Decision-relevant table values require verification against the native source.",
            "Application-level offline flags do not prove host-level network denial.",
        ]
        if suffix == ".docx":
            limitations.append(
                "DOCX logical pages are not mapped to normalized LibreOffice PDF pages."
            )
        manifest = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "status": "completed",
            "source": {
                "document_id": document_id,
                "filename": source.name,
                "suffix": suffix,
                "sha256": source_hash,
                "size_bytes": source.stat().st_size,
                "native_path": native_path.relative_to(temporary_package).as_posix(),
            },
            "mineru": {
                "schema_version": bundle.manifest.schema_version,
                "parser": bundle.manifest.parser,
                "execution": bundle.manifest.execution,
                "manifest_path": "raw/mineru/mineru_manifest.json",
            },
            "exporter": {
                "name": "Knowhere Codex review package exporter",
                "version": "0.1.0",
                "git_commit": _git_commit(),
            },
            "conversion": conversion,
            "counts": {
                "blocks": len(blocks),
                "tables": len(table_results),
                "pages": len(rendered_pages),
                "findings": len(findings),
            },
            "offline": {
                "requested": request.offline,
                "verified": bool(
                    bundle.manifest.execution.get("offline_verified", False)
                ),
            },
            "warnings": warnings,
            "limitations": limitations,
            "artifacts": _artifact_inventory(temporary_package),
        }
        manifest_path = temporary_package / "metadata" / "manifest.json"
        _write_json_atomic(manifest_path, manifest)
        _finalize_directory(
            temporary_package,
            final_package,
            force=request.force,
        )
        if request.keep_work_dir:
            retained_work = work_root
        elif work_root.exists():
            shutil.rmtree(work_root)
        return ReviewPackageResult(
            package_root=final_package,
            manifest_path=final_package / "metadata" / "manifest.json",
            document_id=document_id,
            block_count=len(blocks),
            table_count=len(table_results),
            page_count=len(rendered_pages),
            work_dir=retained_work,
        )
    except Exception:
        if request.keep_work_dir:
            _preserve_failed_work(
                temporary_package=temporary_package,
                work_root=work_root,
                final_package=final_package,
            )
        else:
            if temporary_package.exists():
                shutil.rmtree(temporary_package)
            if work_root.exists():
                shutil.rmtree(work_root)
        raise
