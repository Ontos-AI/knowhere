"""Shared helpers for the Publication benchmark command line tools."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

EXIT_OK: int = 0
EXIT_GATE_FAILED: int = 1
EXIT_NOT_IMPLEMENTED: int = 2
EXIT_GUARD_REFUSED: int = 3


def bootstrap_repository_path() -> Path:
    """Make the repository root importable when a script runs directly."""
    repository_root = Path(__file__).resolve().parents[2]
    value = str(repository_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    return repository_root


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one machine-readable JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    """Append one JSON line to a benchmark record file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON-lines file, returning an empty list when absent."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            records.append(json.loads(stripped))
    return records


def print_result(payload: Mapping[str, Any]) -> None:
    """Print one machine-readable command result."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def resolve_code_commit(repository_root: Path) -> str:
    """Return the current git commit or an explicit placeholder."""
    for command in (
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", "--short", "HEAD"],
    ):
        try:
            completed = subprocess.run(
                command,
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        commit = completed.stdout.strip()
        if commit:
            return commit
    return "unknown-commit"


def resolve_dependency_lock_digest(
    repository_root: Path,
    *,
    lock_file_name: str = "uv.lock",
) -> str:
    """Return the opaque digest of the dependency lock file."""
    from hashlib import sha256

    lock_path = repository_root / lock_file_name
    if not lock_path.is_file():
        return "sha256:missing-lockfile"
    return "sha256:" + sha256(lock_path.read_bytes()).hexdigest()
