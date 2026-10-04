"""Benchmark run identity and comparability.

Baseline and candidate samples require matching environment and input identity.
Independent clones use isolated Redis namespaces derived from their run IDs;
that exact naming protocol is the only exception to namespace equality.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

PUBLICATION_OWNERS: tuple[str, ...] = ("sync", "async")
PUBLICATION_MODES: tuple[str, ...] = ("cold", "warm")

COMPARABILITY_FIELDS: tuple[str, ...] = (
    "code_commit",
    "source_content_digest",
    "dependency_lock_digest",
    "owner",
    "postgres_profile",
    "postgres_version",
    "postgres_settings_digest",
    "redis_namespace",
    "input_digest",
    "clone_source_digest",
    "host_id",
)
SOURCE_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


class RunRecordError(ValueError):
    """Raised when a publication run record violates its contract."""


@dataclass(frozen=True)
class PublicationRunRecord:
    """Identity of one publication benchmark sample."""

    run_id: str
    sample_id: str
    strategy: str
    owner: str
    mode: str
    code_commit: str
    dependency_lock_digest: str
    postgres_profile: str
    postgres_version: str
    postgres_settings_digest: str
    redis_namespace: str
    input_digest: str
    clone_id: str
    clone_source_digest: str
    host_id: str
    started_at: str
    ended_at: str
    outcome: str
    publication_duration_ms: float | None
    publication_attempt_ref: str
    template_content_digest: str | None = None
    source_content_digest: str | None = None

    def __post_init__(self) -> None:
        if self.owner not in PUBLICATION_OWNERS:
            raise RunRecordError(
                f"unsupported owner {self.owner!r}; expected {PUBLICATION_OWNERS}"
            )
        if self.mode not in PUBLICATION_MODES:
            raise RunRecordError(
                f"unsupported mode {self.mode!r}; expected {PUBLICATION_MODES}"
            )
        if not self.input_digest.startswith("sha256:"):
            raise RunRecordError(
                f"run record requires a frozen input digest; got {self.input_digest!r}"
            )
        for field in COMPARABILITY_FIELDS:
            if not str(getattr(self, field)).strip():
                raise RunRecordError(f"run record field {field} must not be empty")
        if (
            self.template_content_digest is not None
            and not self.template_content_digest.startswith("sha256:")
        ):
            raise RunRecordError("template content digest must be a SHA-256 reference")
        if (
            self.source_content_digest is not None
            and not SOURCE_DIGEST_PATTERN.fullmatch(self.source_content_digest)
        ):
            raise RunRecordError("source content digest must be a SHA-256 reference")

    def comparability_fields(self) -> dict[str, str]:
        """Return the fields that must match across compared runs."""
        return {field: str(getattr(self, field)) for field in COMPARABILITY_FIELDS}

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "sample_id": self.sample_id,
            "strategy": self.strategy,
            "owner": self.owner,
            "mode": self.mode,
            "code_commit": self.code_commit,
            "source_content_digest": self.source_content_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "postgres_profile": self.postgres_profile,
            "postgres_version": self.postgres_version,
            "postgres_settings_digest": self.postgres_settings_digest,
            "redis_namespace": self.redis_namespace,
            "input_digest": self.input_digest,
            "clone_id": self.clone_id,
            "clone_source_digest": self.clone_source_digest,
            "host_id": self.host_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "publication_duration_ms": self.publication_duration_ms,
            "publication_attempt_ref": self.publication_attempt_ref,
            "template_content_digest": self.template_content_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PublicationRunRecord":
        duration = payload.get("publication_duration_ms")
        return cls(
            run_id=str(payload["run_id"]),
            sample_id=str(payload["sample_id"]),
            strategy=str(payload["strategy"]),
            owner=str(payload["owner"]),
            mode=str(payload["mode"]),
            code_commit=str(payload["code_commit"]),
            source_content_digest=(
                None
                if payload.get("source_content_digest") is None
                else str(payload["source_content_digest"])
            ),
            dependency_lock_digest=str(payload["dependency_lock_digest"]),
            postgres_profile=str(payload["postgres_profile"]),
            postgres_version=str(payload["postgres_version"]),
            postgres_settings_digest=str(payload["postgres_settings_digest"]),
            redis_namespace=str(payload["redis_namespace"]),
            input_digest=str(payload["input_digest"]),
            clone_id=str(payload["clone_id"]),
            clone_source_digest=str(payload["clone_source_digest"]),
            host_id=str(payload["host_id"]),
            started_at=str(payload["started_at"]),
            ended_at=str(payload["ended_at"]),
            outcome=str(payload["outcome"]),
            publication_duration_ms=(
                None if duration is None else float(str(duration))
            ),
            publication_attempt_ref=str(payload["publication_attempt_ref"]),
            template_content_digest=(
                None
                if payload.get("template_content_digest") is None
                else str(payload["template_content_digest"])
            ),
        )


def find_comparability_conflicts(
    baseline: PublicationRunRecord,
    candidate: PublicationRunRecord,
) -> tuple[str, ...]:
    """Return the comparability fields that differ between two runs."""
    baseline_fields = baseline.comparability_fields()
    candidate_fields = candidate.comparability_fields()
    exceptions = find_comparability_exceptions(baseline, candidate)
    conflicts = tuple(
        field
        for field in COMPARABILITY_FIELDS
        if baseline_fields[field] != candidate_fields[field]
        and field not in exceptions
    )
    if baseline.template_content_digest != candidate.template_content_digest:
        return (*conflicts, "template_content_digest")
    return conflicts


def find_comparability_exceptions(
    baseline: PublicationRunRecord,
    candidate: PublicationRunRecord,
) -> tuple[str, ...]:
    """Report the explicit namespace isolation exception for independent clones."""
    if baseline.redis_namespace == candidate.redis_namespace:
        return ()
    if baseline.run_id == candidate.run_id:
        return ()
    if baseline.redis_namespace != f"publication-benchmark:{baseline.run_id}":
        return ()
    if candidate.redis_namespace != f"publication-benchmark:{candidate.run_id}":
        return ()
    return ("redis_namespace",)


def assert_runs_are_comparable(
    baseline: PublicationRunRecord,
    candidate: PublicationRunRecord,
) -> None:
    """Refuse to compare runs whose non-strategy identity differs."""
    conflicts = find_comparability_conflicts(baseline, candidate)
    if conflicts:
        raise RunRecordError(
            "baseline and candidate runs are not comparable; differing fields: "
            + ", ".join(conflicts)
        )
    if baseline.strategy == candidate.strategy:
        raise RunRecordError(
            "baseline and candidate runs must use different strategies"
        )


def dependency_lock_digest(lock_path: Path) -> str:
    """Return the opaque digest of the dependency lock file."""
    from hashlib import sha256

    return "sha256:" + sha256(lock_path.read_bytes()).hexdigest()


def resolve_source_content_digest(repository_root: Path) -> str:
    """Fingerprint tracked and nonignored untracked source without exposing it."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repository_root,
            capture_output=True,
            check=True,
        )
        deleted = subprocess.run(
            ["git", "ls-files", "--deleted", "-z"],
            cwd=repository_root, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RunRecordError("could not list benchmark source files") from error
    paths = sorted(set(completed.stdout.split(b"\0")) - {b""})
    deleted_paths = set(deleted.stdout.split(b"\0")) - {b""}
    digest = hashlib.sha256()
    for raw_path in paths:
        if raw_path == b".benchmarks" or raw_path.startswith(b".benchmarks/"):
            continue
        path = repository_root / os.fsdecode(raw_path)
        try:
            file_mode = path.lstat().st_mode if raw_path not in deleted_paths else None
            if file_mode is None:
                content = b""
                file_kind = b"deleted"
            elif stat.S_ISLNK(file_mode):
                content = os.readlink(path).encode("utf-8", errors="surrogateescape")
                file_kind = b"link"
            elif stat.S_ISREG(file_mode):
                content = path.read_bytes()
                file_kind = b"file"
            else:
                raise RunRecordError("benchmark source contains an unsupported path")
        except OSError as error:
            raise RunRecordError(
                "benchmark source changed during fingerprinting"
            ) from error
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        digest.update(file_kind)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def host_identity() -> str:
    """Return an opaque host identifier for a benchmark run."""
    import platform
    import socket
    from hashlib import sha256

    raw = f"{platform.system()}|{platform.machine()}|{socket.gethostname()}"
    return "host-" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def docker_setting_digest(settings: Mapping[str, str]) -> str:
    """Return the opaque digest of recorded PostgreSQL settings."""
    from hashlib import sha256

    raw = "\n".join(f"{key}={settings[key]}" for key in sorted(settings))
    return "sha256:" + sha256(raw.encode("utf-8")).hexdigest()


def summarize_owners(records: Sequence[PublicationRunRecord]) -> dict[str, int]:
    """Count records per owner for report summaries."""
    counts: dict[str, int] = {owner: 0 for owner in PUBLICATION_OWNERS}
    for record in records:
        counts[record.owner] = counts.get(record.owner, 0) + 1
    return counts
