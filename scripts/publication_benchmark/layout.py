"""Benchmark filesystem layout.

Every generated benchmark artifact lives under the git-ignored
``.benchmarks/`` tree. Source files never move, and the September production
dump keeps living in its stable Docker named volume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BENCHMARK_ROOT_DIRECTORY_NAME: str = ".benchmarks"
PUBLICATION_DIRECTORY_NAME: str = "publication"
INPUTS_DIRECTORY_NAME: str = "inputs"
CLONES_DIRECTORY_NAME: str = "clones"
REPORTS_DIRECTORY_NAME: str = "reports"
RETRIEVAL_PARITY_DIRECTORY_NAME: str = "retrieval-parity"
DEFAULT_INPUT_NAME: str = "spacex-s1-production"

CLONE_RECORD_FILE_NAME: str = "clone.json"
DATABASE_URL_FILE_NAME: str = "database-url"
FROZEN_MANIFEST_FILE_NAME: str = "manifest.json"
FROZEN_PAYLOAD_FILE_NAME: str = "chunks.json.gz"
POSTGRES_LOG_FILE_NAME: str = "postgres.log"
PUBLICATION_RECORD_FILE_NAME: str = "publication.jsonl"
STATE_BEFORE_FILE_NAME: str = "state-before.json"
STATE_AFTER_FILE_NAME: str = "state-after.json"

SUMMARY_FILE_NAME: str = "summary.json"
SUMMARY_DOCUMENT_FILE_NAME: str = "summary.md"
RESOURCE_FILE_NAME: str = "resource.json"
GATE_RESULTS_FILE_NAME: str = "gate-results.json"

_RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class BenchmarkLayoutError(ValueError):
    """Raised when a benchmark path or identifier is not valid."""


def validate_run_id(run_id: str) -> str:
    """Return a safe run identifier or raise."""
    value = str(run_id or "").strip()
    if not _RUN_ID_PATTERN.match(value):
        raise BenchmarkLayoutError(
            "run-id must match [a-z0-9][a-z0-9._-]{0,63}; "
            f"received {run_id!r}"
        )
    return value


def discover_repository_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking up from ``start``.

    A ``.git`` directory wins over a nested ``pyproject.toml`` so that running a
    benchmark command from ``apps/api`` still resolves the repository root.
    """
    current = (start or Path.cwd()).resolve()
    candidates = (current, *current.parents)
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise BenchmarkLayoutError(f"Could not locate the repository root from {current}")


@dataclass(frozen=True)
class BenchmarkLayout:
    """Resolved benchmark directory layout for one repository checkout."""

    repository_root: Path

    def __post_init__(self) -> None:
        if not self.repository_root.is_absolute():
            raise BenchmarkLayoutError(
                f"repository_root must be absolute; got {self.repository_root}"
            )

    @classmethod
    def from_path(cls, path: Path | None = None) -> "BenchmarkLayout":
        """Build a layout rooted at the repository that contains ``path``."""
        return cls(repository_root=discover_repository_root(path))

    @property
    def benchmark_root(self) -> Path:
        return self.repository_root / BENCHMARK_ROOT_DIRECTORY_NAME

    @property
    def publication_root(self) -> Path:
        return self.benchmark_root / PUBLICATION_DIRECTORY_NAME

    @property
    def inputs_root(self) -> Path:
        return self.publication_root / INPUTS_DIRECTORY_NAME

    @property
    def clones_root(self) -> Path:
        return self.publication_root / CLONES_DIRECTORY_NAME

    @property
    def reports_root(self) -> Path:
        return self.publication_root / REPORTS_DIRECTORY_NAME

    @property
    def retrieval_parity_root(self) -> Path:
        return self.benchmark_root / RETRIEVAL_PARITY_DIRECTORY_NAME

    def frozen_input_directory(self, input_name: str = DEFAULT_INPUT_NAME) -> Path:
        validate_run_id(input_name)
        return self.inputs_root / input_name

    def frozen_manifest_path(self, input_name: str = DEFAULT_INPUT_NAME) -> Path:
        return self.frozen_input_directory(input_name) / "manifest.json"

    def frozen_payload_path(self, input_name: str = DEFAULT_INPUT_NAME) -> Path:
        return self.frozen_input_directory(input_name) / "chunks.json.gz"

    def clone_directory(self, run_id: str) -> Path:
        return self.clones_root / validate_run_id(run_id)

    def clone_record_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / CLONE_RECORD_FILE_NAME

    def database_url_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / DATABASE_URL_FILE_NAME

    def publication_record_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / PUBLICATION_RECORD_FILE_NAME

    def state_before_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / STATE_BEFORE_FILE_NAME

    def state_after_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / STATE_AFTER_FILE_NAME

    def postgres_log_path(self, run_id: str) -> Path:
        return self.clone_directory(run_id) / POSTGRES_LOG_FILE_NAME

    def report_directory(self, run_id: str) -> Path:
        return self.reports_root / validate_run_id(run_id)

    def ensure_clone_directory(self, run_id: str) -> Path:
        directory = self.clone_directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def ensure_report_directory(self, run_id: str) -> Path:
        directory = self.report_directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
