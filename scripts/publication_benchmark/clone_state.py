"""Writable clone identity contract.

Every benchmark sample runs against a separate writable clone that records the
source volume digest, schema revision, PostgreSQL settings, index inventory,
and database identity. The original source volume is never a clone target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, cast

from scripts.publication_benchmark.guards import (
    BenchmarkGuardError,
    assert_clone_record_is_usable,
    assert_clone_target_is_distinct_from_source,
    database_url_digest,
)
from scripts.publication_benchmark.layout import (
    CLONE_RECORD_FILE_NAME,
    validate_run_id,
)

POSTGRES_PROFILES: tuple[str, ...] = ("production", "conservative")
CLONE_STATES: tuple[str, ...] = (
    "creating",
    "ready",
    "reset",
    "sampled",
    "destroyed",
    "failed",
)
USABLE_CLONE_STATES: tuple[str, ...] = ("ready", "reset", "sampled")
FRESH_CLONE_STATES: tuple[str, ...] = ("ready", "reset")
REQUIRED_DATABASE_IDENTITY_FIELDS: tuple[str, ...] = (
    "system_identifier",
    "database_name",
    "database_oid",
    "size_bytes",
)


class CloneRecordError(ValueError):
    """Raised when a clone record violates its contract."""


@dataclass(frozen=True)
class SourceVolumeIdentity:
    """Immutable identity of the production dump volume used as clone source."""

    volume_name: str
    digest: str
    container_name: str
    database_name: str
    database_url_host: str
    database_url_port: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "volume_name": self.volume_name,
            "digest": self.digest,
            "container_name": self.container_name,
            "database_name": self.database_name,
            "database_url_host": self.database_url_host,
            "database_url_port": self.database_url_port,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceVolumeIdentity":
        return cls(
            volume_name=str(payload["volume_name"]),
            digest=str(payload["digest"]),
            container_name=str(payload["container_name"]),
            database_name=str(payload["database_name"]),
            database_url_host=str(payload["database_url_host"]),
            database_url_port=int(payload["database_url_port"]),
        )


@dataclass(frozen=True)
class CloneRecord:
    """Recorded identity and state of one writable benchmark clone."""

    clone_id: str
    run_id: str
    created_at: str
    profile: str
    postgres_image: str
    postgres_version: str
    schema_revision: str
    source: SourceVolumeIdentity
    data_volume: str
    container_name: str
    port: int
    database_name: str
    database_url_digest: str
    database_identity: Mapping[str, Any]
    server_settings: Mapping[str, str]
    index_inventory: tuple[Mapping[str, Any], ...]
    deviations: tuple[str, ...]
    state: str

    def __post_init__(self) -> None:
        validate_run_id(self.clone_id)
        validate_run_id(self.run_id)
        if self.clone_id != self.run_id:
            raise CloneRecordError(
                f"clone_id {self.clone_id!r} must equal run_id {self.run_id!r}"
            )
        if self.profile not in POSTGRES_PROFILES:
            raise CloneRecordError(
                f"unsupported PostgreSQL profile {self.profile!r}; "
                f"expected one of {POSTGRES_PROFILES}"
            )
        if self.state not in CLONE_STATES:
            raise CloneRecordError(
                f"unsupported clone state {self.state!r}; expected one of "
                f"{CLONE_STATES}"
            )
        if not self.schema_revision.strip():
            raise CloneRecordError("clone record requires a schema revision")
        if not self.database_url_digest.strip():
            raise CloneRecordError("clone record requires a database URL digest")
        if not self.source.digest.strip():
            raise CloneRecordError("clone record requires a source volume digest")
        if not self.server_settings:
            raise CloneRecordError("clone record requires recorded PostgreSQL settings")
        if not self.index_inventory:
            raise CloneRecordError("clone record requires an index inventory")
        missing_identity = [
            field
            for field in REQUIRED_DATABASE_IDENTITY_FIELDS
            if field not in self.database_identity
        ]
        if missing_identity:
            raise CloneRecordError(
                f"clone record database identity is missing {missing_identity}"
            )
        assert_clone_target_is_distinct_from_source(
            source_volume_name=self.source.volume_name,
            clone_volume_name=self.data_volume,
            clone_container_name=self.container_name,
            purpose="clone record",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "clone_id": self.clone_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "profile": self.profile,
            "postgres_image": self.postgres_image,
            "postgres_version": self.postgres_version,
            "schema_revision": self.schema_revision,
            "source": self.source.to_dict(),
            "data_volume": self.data_volume,
            "container_name": self.container_name,
            "port": self.port,
            "database_name": self.database_name,
            "database_url_digest": self.database_url_digest,
            "database_identity": dict(self.database_identity),
            "server_settings": dict(self.server_settings),
            "index_inventory": [dict(entry) for entry in self.index_inventory],
            "deviations": list(self.deviations),
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CloneRecord":
        return cls(
            clone_id=str(payload["clone_id"]),
            run_id=str(payload["run_id"]),
            created_at=str(payload["created_at"]),
            profile=str(payload["profile"]),
            postgres_image=str(payload["postgres_image"]),
            postgres_version=str(payload["postgres_version"]),
            schema_revision=str(payload["schema_revision"]),
            source=SourceVolumeIdentity.from_dict(
                cast(Mapping[str, Any], payload["source"])
            ),
            data_volume=str(payload["data_volume"]),
            container_name=str(payload["container_name"]),
            port=int(payload["port"]),
            database_name=str(payload["database_name"]),
            database_url_digest=str(payload["database_url_digest"]),
            database_identity=dict(cast(Mapping[str, Any], payload["database_identity"])),
            server_settings={
                str(key): str(value)
                for key, value in cast(
                    Mapping[str, Any], payload["server_settings"]
                ).items()
            },
            index_inventory=tuple(
                dict(cast(Mapping[str, Any], entry))
                for entry in cast(list[Any], payload["index_inventory"])
            ),
            deviations=tuple(str(item) for item in payload.get("deviations", [])),
            state=str(payload["state"]),
        )

    def with_state(self, state: str) -> "CloneRecord":
        """Return a copy of this record in a new lifecycle state."""
        if state not in CLONE_STATES:
            raise CloneRecordError(f"unsupported clone state {state!r}")
        return replace(self, state=state)

    def settings_digest(self) -> str:
        """Return the opaque digest of the recorded PostgreSQL settings."""
        from hashlib import sha256

        raw = "\n".join(
            f"{key}={self.server_settings[key]}"
            for key in sorted(self.server_settings)
        )
        return "sha256:" + sha256(raw.encode("utf-8")).hexdigest()


def clone_data_volume_name(run_id: str) -> str:
    """Return the dedicated Docker volume name for one clone."""
    return f"knowhere-bench-clone-{validate_run_id(run_id)}"


def clone_container_name(run_id: str) -> str:
    """Return the dedicated Docker container name for one clone."""
    return f"knowhere-bench-clone-{validate_run_id(run_id)}"


def write_clone_record(path: Path, record: CloneRecord) -> None:
    """Write one clone record to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_clone_record(path: Path) -> CloneRecord:
    """Read one clone record from disk."""
    if not path.is_file():
        raise CloneRecordError(f"clone record not found: {path}")
    return CloneRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def find_listed_clone(
    *,
    clones_root: Path,
    run_id: str,
    database_url: str,
) -> CloneRecord:
    """Return the listed clone for a run identifier or refuse the target."""
    path = clones_root / validate_run_id(run_id) / CLONE_RECORD_FILE_NAME
    if not path.is_file():
        raise BenchmarkGuardError(
            f"unlisted clone: no clone record for run {run_id!r} under "
            f"{clones_root}"
        )
    record = read_clone_record(path)
    assert_clone_record_is_usable(
        record,
        run_id=run_id,
        database_url=database_url,
        purpose="clone lookup",
    )
    return record


def record_for_database_url(record: CloneRecord, database_url: str) -> CloneRecord:
    """Return a copy of a record bound to one database URL digest."""
    return replace(record, database_url_digest=database_url_digest(database_url))
