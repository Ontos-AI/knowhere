from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from loguru import logger

from shared.services.storage.job_file_storage import JobFileStorage
from shared.services.storage.storage_adapter import StorageAdapter

_EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
_EXCLUDED_DIR_NAMES = {"tmp", "temp", "__pycache__"}
_CLIENT_ARTIFACT_DIRS = {"images", "tables"}


@dataclass(frozen=True)
class UploadedResultBundle:
    zip_key: str
    raw_prefix: str
    raw_files: dict[str, str]


class ResultStorage(Protocol):
    def upload(
        self, *, job_id: str, result_dir: str, zip_file_path: str
    ) -> UploadedResultBundle:
        raise NotImplementedError

    def generate_artifact_url(
        self, *, job_id: str, artifact_ref: str, expires_in: int = 3600
    ) -> str | None:
        raise NotImplementedError

    def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
        raise NotImplementedError


class JobResultStorage:
    def __init__(
        self,
        *,
        results_bucket: str | None = None,
        storage_adapter: StorageAdapter | None = None,
    ) -> None:
        self._job_file_storage = JobFileStorage(
            storage_adapter=storage_adapter,
            results_bucket=results_bucket,
        )
        self.results_bucket = self._job_file_storage.results_bucket

    def build_zip_key(self, *, job_id: str) -> str:
        return self._job_file_storage.build_result_zip_key(job_id=job_id)

    def build_raw_prefix(self, *, job_id: str) -> str:
        return self._job_file_storage.build_result_raw_prefix(job_id=job_id)

    def build_raw_key(self, *, job_id: str, relative_path: str) -> str:
        normalized = self._normalize_raw_relative_path(relative_path)
        if not normalized:
            raise ValueError(f"Invalid result raw artifact path: {relative_path}")
        return f"{self.build_raw_prefix(job_id=job_id)}{normalized}"

    def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
        normalized = self._normalize_raw_relative_path(artifact_ref)
        if not normalized:
            return None
        parts = normalized.split("/")
        if len(parts) < 2 or parts[0] not in _CLIENT_ARTIFACT_DIRS:
            return None
        return normalized

    def upload(
        self, *, job_id: str, result_dir: str, zip_file_path: str
    ) -> UploadedResultBundle:
        result_path = Path(result_dir)
        if not result_path.is_dir():
            raise ValueError(f"Result directory does not exist: {result_dir}")

        zip_path = Path(zip_file_path)
        if not zip_path.is_file():
            raise ValueError(f"Result ZIP file does not exist: {zip_file_path}")
        zip_key = self.build_zip_key(job_id=job_id)
        self._job_file_storage.upload_local_file(
            str(zip_path),
            zip_key,
            bucket=self.results_bucket,
        )
        self._cleanup_file(zip_path)

        raw_files: dict[str, str] = {}
        for file_path in self._iter_raw_files(result_path):
            relative_path = file_path.relative_to(result_path).as_posix()
            raw_key = self.build_raw_key(job_id=job_id, relative_path=relative_path)
            self._job_file_storage.upload_local_file(
                str(file_path),
                raw_key,
                bucket=self.results_bucket,
            )
            raw_files[relative_path] = raw_key

        return UploadedResultBundle(
            zip_key=zip_key,
            raw_prefix=self.build_raw_prefix(job_id=job_id),
            raw_files=raw_files,
        )

    def generate_url(self, *, storage_key: str, expires_in: int = 3600) -> str | None:
        return self._job_file_storage.generate_download_url(
            storage_key,
            bucket=self.results_bucket,
            expires_in=expires_in,
        )["download_url"]

    def generate_artifact_url(
        self, *, job_id: str, artifact_ref: str, expires_in: int = 3600
    ) -> str | None:
        normalized_ref = self.normalize_artifact_ref(artifact_ref)
        if not normalized_ref:
            return None
        return self.generate_url(
            storage_key=self.build_raw_key(job_id=job_id, relative_path=normalized_ref),
            expires_in=expires_in,
        )

    def _iter_raw_files(self, result_dir: Path) -> Iterator[Path]:
        for root, dir_names, file_names in os.walk(result_dir):
            dir_names[:] = [
                dir_name
                for dir_name in dir_names
                if not self._is_excluded_dir(dir_name)
            ]
            for file_name in file_names:
                if self._is_excluded_file(file_name):
                    continue
                yield Path(root) / file_name

    def _normalize_raw_relative_path(self, relative_path: str | None) -> str | None:
        if not relative_path:
            return None
        normalized = str(relative_path).strip().replace("\\", "/").lstrip("/")
        parts = [
            part for part in normalized.split("/") if part and part not in {".", ".."}
        ]
        if not parts:
            return None
        if any(self._is_excluded_dir(part) for part in parts[:-1]):
            return None
        if self._is_excluded_file(parts[-1]):
            return None
        return "/".join(parts)

    def _is_excluded_file(self, file_name: str) -> bool:
        return file_name in _EXCLUDED_FILE_NAMES or file_name.startswith(".")

    def _is_excluded_dir(self, dir_name: str) -> bool:
        return dir_name in _EXCLUDED_DIR_NAMES or dir_name.startswith(".")

    def _cleanup_file(self, file_path: Path) -> None:
        try:
            file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"Failed to clean up result file {file_path}: {exc}")


def get_result_storage() -> ResultStorage:
    return JobResultStorage()
