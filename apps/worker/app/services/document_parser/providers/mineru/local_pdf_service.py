"""Materialize validated local MinerU artifacts into the cloud-compatible shape."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from app.services.common.file_loading import is_remote
from app.services.document_parser.providers.mineru.local_process import (
    LocalMinerURequest,
    LocalMinerURunner,
)
from shared.core.config import settings


def _copy_confined_images(source: Path, destination: Path) -> None:
    root = source.resolve(strict=True)
    destination.mkdir(parents=True)
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        resolved = item.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("Local MinerU image path escapes the validated directory")
        target = destination / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, target)


def _publish(staged: Path, output: Path) -> None:
    backup_root = staged.parent / "backups"
    installed: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for source in sorted(staged.iterdir(), key=lambda path: path.name):
            destination = output / source.name
            if destination.exists():
                backup_root.mkdir(exist_ok=True)
                backup = backup_root / source.name
                destination.replace(backup)
                backups.append((backup, destination))
            source.replace(destination)
            installed.append(destination)
    except Exception:
        for destination in reversed(installed):
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
        for backup, destination in reversed(backups):
            backup.replace(destination)
        raise


def parse_via_local(
    pdf_path: str,
    filename: str,
    output_dir: str,
    *,
    s3_key: str | None = None,
) -> None:
    """Run local MinerU once and atomically expose ``full.md`` and images."""

    del s3_key
    if is_remote(pdf_path):
        raise ValueError("Local MinerU provider requires a local file")
    source = Path(pdf_path).expanduser().resolve(strict=False)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("Local MinerU provider requires an existing local PDF file")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Local MinerU filename must use the PDF extension")
    project_value = settings.MINERU_LOCAL_PROJECT_PATH.strip()
    if not project_value:
        raise ValueError("Local MinerU project path is not configured")
    uv_value = settings.MINERU_LOCAL_UV_EXECUTABLE.strip()
    if not uv_value:
        raise ValueError("Local MinerU uv executable is not configured")

    output = Path(output_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    run_root = output / f".mineru-local-{uuid.uuid4().hex}"
    artifact_root = run_root / "artifacts"
    staged = run_root / "publish"
    run_root.mkdir()
    try:
        runner = LocalMinerURunner(
            project_path=Path(project_value),
            uv_executable=uv_value,
            timeout_seconds=settings.MINERU_LOCAL_TIMEOUT_SECONDS,
            max_log_chars=settings.MINERU_LOCAL_MAX_LOG_CHARS,
        )
        bundle = runner.run(
            LocalMinerURequest(
                source_path=source,
                output_root=artifact_root,
                backend=settings.MINERU_LOCAL_BACKEND,
                method=settings.MINERU_LOCAL_METHOD,
                language=settings.MINERU_LOCAL_LANGUAGE,
                offline=settings.MINERU_LOCAL_OFFLINE,
            )
        )
        staged.mkdir()
        shutil.copy2(bundle.markdown_path, staged / "full.md")
        if bundle.images_dir.is_dir():
            _copy_confined_images(bundle.images_dir, staged / "images")
        log_path = artifact_root.parent / "logs" / "mineru.log"
        if not log_path.is_file():
            raise ValueError("Local MinerU did not produce its sanitized log")
        logs = staged / "logs"
        logs.mkdir()
        shutil.copy2(log_path, logs / "mineru.log")
        _publish(staged, output)
    finally:
        if run_root.exists():
            shutil.rmtree(run_root)
