"""Task-scoped workspace helpers for worker-side Document Ingestion."""

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    FileSystemException,
    SystemSettingInvalidException,
    SystemSettingMissingException,
)
from shared.services.storage.job_file_storage import JobFileStorage

CleanupTaskWorkspace = Callable[[str | None], bool]


@dataclass(frozen=True)
class TemporaryParseWorkspace:
    """Task-local folders for parser input, parser output, and ZIP generation."""

    root_dir: str
    input_dir: str
    output_dir: str

    @classmethod
    def create(cls, job_id: str) -> "TemporaryParseWorkspace":
        root_dir = create_task_workspace(job_id)
        input_dir = os.path.join(root_dir, "input")
        output_dir = os.path.join(root_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Task workspace ready: job_id={job_id}, workspace={root_dir}")
        return cls(root_dir=root_dir, input_dir=input_dir, output_dir=output_dir)

    def cleanup(
        self,
        cleanup_workspace: CleanupTaskWorkspace | None = None,
    ) -> bool:
        resolved_cleanup = cleanup_workspace or cleanup_task_workspace
        return resolved_cleanup(self.root_dir)


def cleanup_temp_file(file_path: str | None) -> None:
    """Best-effort cleanup for temp files created during parsing."""
    if not file_path:
        return

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError as exc:
        logger.warning(f"Failed to cleanup temp file {file_path}: {exc}")


def cleanup_task_workspace(workspace_dir: str | None) -> bool:
    """Best-effort cleanup for a task-scoped temporary workspace."""
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return False

    try:
        shutil.rmtree(workspace_dir)
        logger.info(f"Task workspace cleaned up: {workspace_dir}")
        return True
    except OSError as exc:
        logger.warning(f"Failed to cleanup task workspace {workspace_dir}: {exc}")
        return False


def create_task_workspace(job_id: str) -> str:
    """Create a temporary workspace for a single parse task."""
    temp_root = getattr(settings, "TMP_PATH", "/tmp")
    if not temp_root:
        raise SystemSettingMissingException(
            user_message="System configuration error",
            internal_message="TMP_PATH not configured",
        )

    if not os.path.isabs(temp_root):
        raise SystemSettingInvalidException(
            user_message="System configuration error",
            internal_message=f"TMP_PATH must be absolute path, current value: {temp_root}",
        )

    try:
        os.makedirs(temp_root, exist_ok=True)
        return tempfile.mkdtemp(
            prefix=f"document_ingestion_task_{job_id}_",
            dir=temp_root,
        )
    except (OSError, PermissionError) as exc:
        raise FileSystemException(
            user_message="System error preparing temporary storage",
            operation="create_temp_workspace",
            internal_message=f"Failed to create task workspace in {temp_root}",
            original_exception=exc,
        ) from exc


def download_s3_file_to_temp(s3_key: str, file_ext: str, temp_dir: str) -> str:
    """Download the source file from object storage into the task workspace."""
    storage = JobFileStorage()
    return storage.download_upload_to_temp(
        s3_key,
        suffix=file_ext,
        temp_dir=temp_dir,
    )
