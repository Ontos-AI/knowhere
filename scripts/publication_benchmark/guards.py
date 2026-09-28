"""Refusal rules for benchmark commands.

Benchmark commands may only touch a listed, writable clone. They must never
read or write the original source database, a read-only URL, an unlisted
database, or an input whose digest is missing.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from scripts.publication_benchmark.clone_state import (
        CloneRecord,
        SourceVolumeIdentity,
    )
    from scripts.publication_benchmark.frozen_input import FrozenInputManifest

READ_ONLY_USER_MARKERS: tuple[str, ...] = ("readonly", "read_only", "read-only")
READ_ONLY_QUERY_MARKERS: tuple[str, ...] = (
    "default_transaction_read_only=on",
    "target_session_attrs=read-only",
)
SUPPORTED_DRIVERS: tuple[str, ...] = ("postgresql", "postgresql+psycopg2")


class BenchmarkGuardError(RuntimeError):
    """Raised when a benchmark command would touch a forbidden target."""


def database_url_digest(database_url: str) -> str:
    """Return the opaque digest recorded for one database URL."""
    return "sha256:" + sha256(database_url.strip().encode("utf-8")).hexdigest()


def assert_writable_database_url(database_url: str, *, purpose: str) -> None:
    """Refuse read-only or non-PostgreSQL benchmark targets."""
    if not str(database_url).strip():
        raise BenchmarkGuardError(f"{purpose}: database URL is empty")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise BenchmarkGuardError(
            f"{purpose}: benchmark targets must be PostgreSQL; got "
            f"{url.get_backend_name()}"
        )
    username = str(url.username or "").lower()
    if any(marker in username for marker in READ_ONLY_USER_MARKERS):
        raise BenchmarkGuardError(
            f"{purpose}: refusing a read-only database URL (user={url.username!r})"
        )
    query_text = " ".join(
        f"{key}={value}" for key, value in sorted(dict(url.query).items())
    ).lower()
    if any(marker in query_text for marker in READ_ONLY_QUERY_MARKERS):
        raise BenchmarkGuardError(
            f"{purpose}: refusing a read-only database URL (query={query_text!r})"
        )


def assert_not_source_database(
    database_url: str,
    *,
    source: "SourceVolumeIdentity",
    purpose: str,
) -> None:
    """Refuse the original source database endpoint."""
    url = make_url(database_url)
    database_name = str(url.database or "")
    if database_name != source.database_name:
        return
    host = str(url.host or "")
    port = int(url.port or 5432)
    if host == source.database_url_host or port == source.database_url_port:
        raise BenchmarkGuardError(
            f"{purpose}: refusing the benchmark source database "
            f"(database={database_name!r} host={host!r} port={port})"
        )


def assert_clone_record_is_usable(
    record: "CloneRecord",
    *,
    run_id: str,
    database_url: str,
    purpose: str,
) -> None:
    """Refuse an unlisted, stale, or mismatched clone record."""
    if record.run_id != run_id or record.clone_id != run_id:
        raise BenchmarkGuardError(
            f"{purpose}: clone record does not describe run {run_id!r}"
        )
    if record.state not in ("ready", "reset", "sampled"):
        raise BenchmarkGuardError(
            f"{purpose}: clone {run_id!r} is not listed as usable "
            f"(state={record.state!r})"
        )
    if record.database_url_digest != database_url_digest(database_url):
        raise BenchmarkGuardError(
            f"{purpose}: database URL does not match the listed clone "
            f"{run_id!r}"
        )


def assert_clone_target_is_distinct_from_source(
    *,
    source_volume_name: str,
    clone_volume_name: str,
    clone_container_name: str,
    purpose: str,
) -> None:
    """Refuse to reuse the immutable source volume for a benchmark clone."""
    if clone_volume_name == source_volume_name:
        raise BenchmarkGuardError(
            f"{purpose}: refusing to write into the immutable source volume "
            f"{source_volume_name!r}"
        )
    if clone_container_name == source_volume_name:
        raise BenchmarkGuardError(
            f"{purpose}: refusing to reuse the source volume name as a "
            f"container name ({clone_container_name!r})"
        )


def assert_frozen_input_digest(manifest: "FrozenInputManifest | None") -> None:
    """Refuse to sample without a recorded frozen input digest."""
    if manifest is None:
        raise BenchmarkGuardError(
            "missing frozen input digest: freeze the publication input before "
            "running any benchmark sample"
        )
    if not str(manifest.digest or "").startswith("sha256:"):
        raise BenchmarkGuardError(
            f"missing frozen input digest: {manifest.digest!r}"
        )


def assert_strategy_matches_environment(
    requested_strategy: str,
    environment_value: str,
    *,
    purpose: str,
) -> None:
    """Refuse a sample whose recorded strategy differs from the live setting."""
    if str(requested_strategy).strip().lower() != str(environment_value).strip().lower():
        raise BenchmarkGuardError(
            f"{purpose}: requested strategy {requested_strategy!r} does not "
            f"match KNOWHERE_PUBLICATION_STRATEGY {environment_value!r}"
        )


def read_database_url_file(path: Path, *, purpose: str) -> str:
    """Read a single-line database URL file and validate it as writable."""
    if not path.is_file():
        raise BenchmarkGuardError(f"{purpose}: database URL file not found: {path}")
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise BenchmarkGuardError(
            f"{purpose}: database URL file must contain exactly one line: {path}"
        )
    assert_writable_database_url(lines[0], purpose=purpose)
    return lines[0]
