"""Frozen publication input contract.

The frozen input is the already parsed production-shaped SpaceX publication
payload: 922 chunks (670 text, 99 image, 153 table), metadata on every chunk,
artifact paths on 252 chunks, 830 map units, and approximately 88,551 token
rows. The payload never embeds artifact file bytes.

A manifest records the schema version, source revision, counts, content digest,
generation timestamp, and an opaque corpus fingerprint. Any payload change
breaks the digest and invalidates every benchmark result that used the input.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, cast

from scripts.publication_benchmark.layout import (
    FROZEN_MANIFEST_FILE_NAME,
    FROZEN_PAYLOAD_FILE_NAME,
)

FROZEN_INPUT_SCHEMA_VERSION: str = "publication-frozen-input/1"
PUBLICATION_INPUT_SCHEMA_VERSION: str = "publication-input/1"
DIGEST_PREFIX: str = "sha256:"

_CHUNK_TYPES: tuple[str, ...] = ("text", "image", "table")
_ARTIFACT_PATH_PREFIXES: tuple[str, ...] = ("images/", "tables/")
_EMBEDDED_BYTES_KEYS: frozenset[str] = frozenset(
    {
        "artifact_bytes",
        "artifact_payload",
        "binary",
        "blob",
        "bytes",
        "content_bytes",
        "file_bytes",
        "image_base64",
        "image_bytes",
        "raw_bytes",
        "raw_payload",
        "payload_bytes",
    }
)
_MIN_SUSPICIOUS_BASE64_LENGTH: int = 1024


class FrozenInputError(ValueError):
    """Raised when a frozen publication input violates its contract."""


@dataclass(frozen=True)
class FrozenInputExpectations:
    """Documented production-shaped counts for a frozen publication input."""

    chunks: int
    text_chunks: int
    image_chunks: int
    table_chunks: int
    chunks_with_metadata: int
    chunks_with_artifact_path: int
    map_units: int
    token_rows: int
    token_row_tolerance_ratio: float = 0.0

    def token_row_bounds(self) -> tuple[float, float]:
        margin = self.token_rows * self.token_row_tolerance_ratio
        return (self.token_rows - margin, self.token_rows + margin)


SPACEX_S1_PRODUCTION_EXPECTATIONS = FrozenInputExpectations(
    chunks=922,
    text_chunks=670,
    image_chunks=99,
    table_chunks=153,
    chunks_with_metadata=922,
    chunks_with_artifact_path=252,
    map_units=830,
    token_rows=88_551,
    token_row_tolerance_ratio=0.01,
)


@dataclass(frozen=True)
class FrozenInputCounts:
    """Counts derived from a payload plus counts recorded from the source."""

    chunks: int
    text_chunks: int
    image_chunks: int
    table_chunks: int
    chunks_with_metadata: int
    chunks_with_artifact_path: int
    map_units: int
    token_rows: int

    def to_dict(self) -> dict[str, int]:
        return {
            "chunks": self.chunks,
            "text_chunks": self.text_chunks,
            "image_chunks": self.image_chunks,
            "table_chunks": self.table_chunks,
            "chunks_with_metadata": self.chunks_with_metadata,
            "chunks_with_artifact_path": self.chunks_with_artifact_path,
            "map_units": self.map_units,
            "token_rows": self.token_rows,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FrozenInputCounts":
        missing = [
            field
            for field in (
                "chunks",
                "text_chunks",
                "image_chunks",
                "table_chunks",
                "chunks_with_metadata",
                "chunks_with_artifact_path",
                "map_units",
                "token_rows",
            )
            if field not in payload
        ]
        if missing:
            raise FrozenInputError(f"frozen input counts are missing {missing}")
        return cls(
            chunks=int(payload["chunks"]),
            text_chunks=int(payload["text_chunks"]),
            image_chunks=int(payload["image_chunks"]),
            table_chunks=int(payload["table_chunks"]),
            chunks_with_metadata=int(payload["chunks_with_metadata"]),
            chunks_with_artifact_path=int(payload["chunks_with_artifact_path"]),
            map_units=int(payload["map_units"]),
            token_rows=int(payload["token_rows"]),
        )


def canonical_chunk_sort_key(chunk: Mapping[str, Any]) -> tuple[int, str]:
    """Return the canonical ordering key for one publication chunk."""
    try:
        order = int(chunk.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    return (order, str(chunk.get("chunk_id") or ""))


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize a payload to its deterministic byte representation."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 digest of a canonical payload."""
    return DIGEST_PREFIX + sha256(canonical_payload_bytes(payload)).hexdigest()


def chunks_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the validated chunk list of a publication payload."""
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list):
        raise FrozenInputError("publication payload must contain a 'chunks' list")
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, dict):
            raise FrozenInputError(f"chunk {index} is not an object")
        chunks.append(cast(dict[str, Any], chunk))
    return chunks


def chunk_type_of(chunk: Mapping[str, Any]) -> str:
    """Return the normalized chunk type of one payload chunk."""
    raw_type = chunk.get("type") or chunk.get("chunk_type") or "text"
    return str(raw_type).strip().lower()


def has_artifact_path(chunk: Mapping[str, Any]) -> bool:
    """Return whether a chunk carries an artifact pointer instead of bytes."""
    metadata = chunk.get("metadata")
    metadata_map = metadata if isinstance(metadata, dict) else {}
    if str(metadata_map.get("file_path") or chunk.get("file_path") or "").strip():
        return True
    source_path = str(metadata_map.get("path") or chunk.get("path") or "").strip()
    return source_path.startswith(_ARTIFACT_PATH_PREFIXES)


_MAX_PROHIBITED_PATH_SAMPLES: int = 50
_MAX_PROHIBITED_CONTENT_SAMPLES: int = 20
_MIN_PROHIBITED_CONTENT_LENGTH: int = 12
_MAX_PROHIBITED_CONTENT_LENGTH: int = 200


def get_prohibited_payload_values(
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return payload values that must never appear in benchmark reports."""
    paths: list[str] = []
    contents: list[str] = []
    for chunk in chunks_of(payload):
        metadata = chunk.get("metadata")
        metadata_map = metadata if isinstance(metadata, dict) else {}
        for candidate in (
            metadata_map.get("path"),
            metadata_map.get("file_path"),
            chunk.get("path"),
            chunk.get("file_path"),
        ):
            text = str(candidate or "").strip()
            if text and text not in paths:
                paths.append(text)
        content = str(chunk.get("content") or chunk.get("text") or "").strip()
        if (
            _MIN_PROHIBITED_CONTENT_LENGTH
            <= len(content)
            <= _MAX_PROHIBITED_CONTENT_LENGTH
            and content not in contents
        ):
            contents.append(content)
    selected = paths[:_MAX_PROHIBITED_PATH_SAMPLES] + contents[
        :_MAX_PROHIBITED_CONTENT_SAMPLES
    ]
    return tuple(selected)


def measure_payload_counts(payload: Mapping[str, Any]) -> FrozenInputCounts:
    """Derive the offline-verifiable counts of a payload."""
    chunks = chunks_of(payload)
    type_counts = {chunk_type: 0 for chunk_type in _CHUNK_TYPES}
    unknown_types: list[str] = []
    with_metadata = 0
    with_artifact_path = 0
    for chunk in chunks:
        chunk_type = chunk_type_of(chunk)
        if chunk_type in type_counts:
            type_counts[chunk_type] += 1
        else:
            unknown_types.append(chunk_type)
        metadata = chunk.get("metadata")
        if isinstance(metadata, dict) and metadata:
            with_metadata += 1
        if has_artifact_path(chunk):
            with_artifact_path += 1
    if unknown_types:
        raise FrozenInputError(
            f"payload contains unsupported chunk types: {sorted(set(unknown_types))}"
        )
    return FrozenInputCounts(
        chunks=len(chunks),
        text_chunks=type_counts["text"],
        image_chunks=type_counts["image"],
        table_chunks=type_counts["table"],
        chunks_with_metadata=with_metadata,
        chunks_with_artifact_path=with_artifact_path,
        map_units=0,
        token_rows=0,
    )


def find_embedded_artifact_bytes(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return dotted paths of payload entries that embed artifact file bytes."""
    violations: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text.lower() in _EMBEDDED_BYTES_KEYS:
                    violations.append(child_path)
                walk(value, child_path)
            return
        if isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
            return
        if isinstance(node, (bytes, bytearray, memoryview)):
            violations.append(path)
            return
        if isinstance(node, str) and len(node) >= _MIN_SUSPICIOUS_BASE64_LENGTH:
            compact = node.strip()
            if compact.startswith("data:"):
                violations.append(path)

    walk(dict(payload), "")
    return tuple(violations)


def assert_canonical_ordering(payload: Mapping[str, Any]) -> None:
    """Fail unless the payload chunk list is already canonically ordered."""
    chunks = chunks_of(payload)
    ordered = sorted(chunks, key=canonical_chunk_sort_key)
    canonical_ids = [str(chunk.get("chunk_id") or "") for chunk in ordered]
    actual_ids = [str(chunk.get("chunk_id") or "") for chunk in chunks]
    if canonical_ids != actual_ids:
        raise FrozenInputError(
            "payload chunk ordering is not canonical "
            "(expected sort by order then chunk_id)"
        )


def validate_payload(
    payload: Mapping[str, Any],
    *,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
    recorded_counts: FrozenInputCounts | None = None,
) -> FrozenInputCounts:
    """Validate one payload against the frozen input contract."""
    schema_version = payload.get("schema_version")
    if schema_version not in (None, PUBLICATION_INPUT_SCHEMA_VERSION):
        raise FrozenInputError(
            f"unsupported payload schema version {schema_version!r}; "
            f"expected {PUBLICATION_INPUT_SCHEMA_VERSION!r}"
        )

    embedded = find_embedded_artifact_bytes(payload)
    if embedded:
        raise FrozenInputError(
            "publication payload embeds artifact file bytes at "
            f"{list(embedded[:5])}"
        )

    assert_canonical_ordering(payload)
    counts = measure_payload_counts(payload)
    observed = counts.to_dict()
    expected = {
        "chunks": expectations.chunks,
        "text_chunks": expectations.text_chunks,
        "image_chunks": expectations.image_chunks,
        "table_chunks": expectations.table_chunks,
        "chunks_with_metadata": expectations.chunks_with_metadata,
        "chunks_with_artifact_path": expectations.chunks_with_artifact_path,
    }
    mismatches = {
        field: (observed[field], expected[field])
        for field in expected
        if observed[field] != expected[field]
    }
    if mismatches:
        raise FrozenInputError(
            "publication payload counts do not match the frozen input contract: "
            + ", ".join(
                f"{field} observed={actual} expected={wanted}"
                for field, (actual, wanted) in sorted(mismatches.items())
            )
        )

    effective_counts = counts
    if recorded_counts is not None:
        recorded = recorded_counts.to_dict()
        for field in ("chunks", "text_chunks", "image_chunks", "table_chunks",
                      "chunks_with_metadata", "chunks_with_artifact_path"):
            if recorded[field] != observed[field]:
                raise FrozenInputError(
                    f"manifest count {field}={recorded[field]} does not match "
                    f"payload count {observed[field]}"
                )
        if recorded["map_units"] != expectations.map_units:
            raise FrozenInputError(
                f"manifest map_units={recorded['map_units']} does not match the "
                f"frozen contract {expectations.map_units}"
            )
        low, high = expectations.token_row_bounds()
        if not (low <= recorded["token_rows"] <= high):
            raise FrozenInputError(
                f"manifest token_rows={recorded['token_rows']} is outside the "
                f"documented range [{low}, {high}]"
            )
        effective_counts = FrozenInputCounts(
            chunks=recorded["chunks"],
            text_chunks=recorded["text_chunks"],
            image_chunks=recorded["image_chunks"],
            table_chunks=recorded["table_chunks"],
            chunks_with_metadata=recorded["chunks_with_metadata"],
            chunks_with_artifact_path=recorded["chunks_with_artifact_path"],
            map_units=recorded["map_units"],
            token_rows=recorded["token_rows"],
        )
    return effective_counts


@dataclass(frozen=True)
class FrozenInputManifest:
    """Manifest describing one frozen publication input."""

    schema_version: str
    payload_schema_version: str
    generated_at: str
    source_revision: str
    corpus_fingerprint: str
    digest: str
    payload_file: str
    payload_bytes: int
    payload_gzip_bytes: int
    counts: FrozenInputCounts
    source_database_identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "payload_schema_version": self.payload_schema_version,
            "generated_at": self.generated_at,
            "source_revision": self.source_revision,
            "corpus_fingerprint": self.corpus_fingerprint,
            "digest": self.digest,
            "payload_file": self.payload_file,
            "payload_bytes": self.payload_bytes,
            "payload_gzip_bytes": self.payload_gzip_bytes,
            "counts": self.counts.to_dict(),
            "source_database_identity": self.source_database_identity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FrozenInputManifest":
        required = (
            "schema_version",
            "payload_schema_version",
            "generated_at",
            "source_revision",
            "corpus_fingerprint",
            "digest",
            "payload_file",
            "payload_bytes",
            "payload_gzip_bytes",
            "counts",
            "source_database_identity",
        )
        missing = [field for field in required if field not in payload]
        if missing:
            raise FrozenInputError(f"frozen input manifest is missing {missing}")
        if payload["schema_version"] != FROZEN_INPUT_SCHEMA_VERSION:
            raise FrozenInputError(
                f"unsupported frozen input schema version "
                f"{payload['schema_version']!r}"
            )
        digest = str(payload["digest"])
        if not digest.startswith(DIGEST_PREFIX) or len(digest) <= len(DIGEST_PREFIX):
            raise FrozenInputError(f"invalid frozen input digest {digest!r}")
        return cls(
            schema_version=str(payload["schema_version"]),
            payload_schema_version=str(payload["payload_schema_version"]),
            generated_at=str(payload["generated_at"]),
            source_revision=str(payload["source_revision"]),
            corpus_fingerprint=str(payload["corpus_fingerprint"]),
            digest=digest,
            payload_file=str(payload["payload_file"]),
            payload_bytes=int(payload["payload_bytes"]),
            payload_gzip_bytes=int(payload["payload_gzip_bytes"]),
            counts=FrozenInputCounts.from_dict(
                cast(Mapping[str, Any], payload["counts"])
            ),
            source_database_identity=str(payload["source_database_identity"]),
        )

    def validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
    ) -> FrozenInputCounts:
        """Validate a payload against this manifest and the frozen contract."""
        actual_digest = compute_payload_digest(payload)
        if actual_digest != self.digest:
            raise FrozenInputError(
                f"frozen input digest mismatch: manifest={self.digest} "
                f"payload={actual_digest}"
            )
        canonical_bytes = canonical_payload_bytes(payload)
        if len(canonical_bytes) != self.payload_bytes:
            raise FrozenInputError(
                f"frozen input payload_bytes mismatch: manifest="
                f"{self.payload_bytes} payload={len(canonical_bytes)}"
            )
        return validate_payload(
            payload,
            expectations=expectations,
            recorded_counts=self.counts,
        )


def write_frozen_input(
    input_directory: Path,
    *,
    payload: Mapping[str, Any],
    manifest: FrozenInputManifest,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
) -> None:
    """Write the frozen payload and manifest into the input directory."""
    manifest.validate_payload(payload, expectations=expectations)
    input_directory.mkdir(parents=True, exist_ok=True)
    canonical_bytes = canonical_payload_bytes(payload)
    compressed = gzip.compress(canonical_bytes, compresslevel=6, mtime=0)
    (input_directory / FROZEN_PAYLOAD_FILE_NAME).write_bytes(compressed)
    (input_directory / FROZEN_MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_frozen_input(
    input_directory: Path,
    *,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
) -> tuple[dict[str, Any], FrozenInputManifest]:
    """Load and fully validate a frozen publication input."""
    manifest_path = input_directory / FROZEN_MANIFEST_FILE_NAME
    payload_path = input_directory / FROZEN_PAYLOAD_FILE_NAME
    if not manifest_path.is_file():
        raise FrozenInputError(f"frozen input manifest not found: {manifest_path}")
    if not payload_path.is_file():
        raise FrozenInputError(f"frozen input payload not found: {payload_path}")

    manifest = FrozenInputManifest.from_dict(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    payload = cast(
        dict[str, Any],
        json.loads(gzip.decompress(payload_path.read_bytes()).decode("utf-8")),
    )
    manifest.validate_payload(payload, expectations=expectations)
    gzip_bytes = payload_path.stat().st_size
    if gzip_bytes != manifest.payload_gzip_bytes:
        raise FrozenInputError(
            f"frozen input gzip size mismatch: manifest="
            f"{manifest.payload_gzip_bytes} payload={gzip_bytes}"
        )
    return payload, manifest
