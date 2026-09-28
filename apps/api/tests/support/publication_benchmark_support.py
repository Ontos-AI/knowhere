"""Fixtures for Publication benchmark contract tests."""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[4]
PLAN_DOCUMENT_RELATIVE_PATH: str = (
    "docs/design/publication-performance-optimization-plan.md"
)
SOURCE_VOLUME_NAME: str = "knowhere-prod-restore-data"
SOURCE_CONTAINER_NAME: str = "knowhere-prod-restore-pg"
SOURCE_DATABASE_NAME: str = "knowhere"
SOURCE_DATABASE_PORT: int = 55433

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.publication_benchmark.clone_state import (
    CloneRecord,
    SourceVolumeIdentity,
    clone_container_name,
    clone_data_volume_name,
    write_clone_record,
)
from scripts.publication_benchmark.frozen_input import (
    FROZEN_INPUT_SCHEMA_VERSION,
    PUBLICATION_INPUT_SCHEMA_VERSION,
    FrozenInputCounts,
    FrozenInputExpectations,
    FrozenInputManifest,
    canonical_payload_bytes,
    compute_payload_digest,
    measure_payload_counts,
    write_frozen_input,
)
from scripts.publication_benchmark.guards import database_url_digest
from scripts.publication_benchmark.layout import BenchmarkLayout


def ensure_benchmark_import_path() -> None:
    """Make the repository-root benchmark package importable."""
    value = str(REPOSITORY_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)


def benchmark_layout(root: Path) -> BenchmarkLayout:
    """Build a benchmark layout rooted at a temporary repository."""
    return BenchmarkLayout(repository_root=root.resolve())


def copy_plan_document(repository_root: Path) -> Path:
    """Copy the design document so plan-derived contracts stay testable."""
    destination = repository_root / PLAN_DOCUMENT_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        (REPOSITORY_ROOT / PLAN_DOCUMENT_RELATIVE_PATH).read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return destination


def build_frozen_payload(*, text_chunks: int = 2, artifact_chunks: int = 2) -> dict:
    """Build a small canonical publication payload for contract tests."""
    chunks: list[dict[str, Any]] = []
    order = 0
    for index in range(text_chunks):
        chunks.append(
            {
                "chunk_id": f"text-chunk-{index}",
                "type": "text",
                "content": f"text content {index}",
                "order": order,
                "path": f"bench-source.pdf/Root/Text {index}",
                "metadata": {
                    "path": f"bench-source.pdf/Root/Text {index}",
                    "summary": f"summary {index}",
                    "keywords": [],
                    "tokens": [],
                    "length": 16,
                },
            }
        )
        order += 1
    for index in range(artifact_chunks):
        chunk_type = "image" if index % 2 == 0 else "table"
        prefix = "images" if chunk_type == "image" else "tables"
        suffix = "png" if chunk_type == "image" else "html"
        chunks.append(
            {
                "chunk_id": f"{chunk_type}-chunk-{index}",
                "type": chunk_type,
                "content": f"{chunk_type} content {index}",
                "order": order,
                "path": f"{prefix}/artifact-{index}.{suffix}",
                "metadata": {
                    "path": f"{prefix}/artifact-{index}.{suffix}",
                    "file_path": f"{prefix}/artifact-{index}.{suffix}",
                    "summary": f"{chunk_type} summary {index}",
                    "keywords": [],
                    "tokens": [],
                    "length": 24,
                },
            }
        )
        order += 1
    chunks.sort(key=lambda chunk: (int(chunk["order"]), str(chunk["chunk_id"])))
    return {
        "schema_version": PUBLICATION_INPUT_SCHEMA_VERSION,
        "chunks": chunks,
    }


def expectations_for(
    payload: Mapping[str, Any],
    *,
    map_units: int = 0,
    token_rows: int = 0,
    token_row_tolerance_ratio: float = 0.0,
) -> FrozenInputExpectations:
    """Return payload-matching expectations for contract tests."""
    counts = measure_payload_counts(payload)
    return FrozenInputExpectations(
        chunks=counts.chunks,
        text_chunks=counts.text_chunks,
        image_chunks=counts.image_chunks,
        table_chunks=counts.table_chunks,
        chunks_with_metadata=counts.chunks_with_metadata,
        chunks_with_artifact_path=counts.chunks_with_artifact_path,
        map_units=map_units,
        token_rows=token_rows,
        token_row_tolerance_ratio=token_row_tolerance_ratio,
    )


def build_manifest(
    payload: Mapping[str, Any],
    *,
    map_units: int = 0,
    token_rows: int = 0,
    source_revision: str = "bench-revision",
) -> FrozenInputManifest:
    """Build the manifest that matches one synthetic payload."""
    from gzip import compress

    counts = measure_payload_counts(payload)
    canonical_bytes = canonical_payload_bytes(payload)
    digest = compute_payload_digest(payload)
    return FrozenInputManifest(
        schema_version=FROZEN_INPUT_SCHEMA_VERSION,
        payload_schema_version=PUBLICATION_INPUT_SCHEMA_VERSION,
        generated_at="2026-09-23T00:00:00Z",
        source_revision=source_revision,
        corpus_fingerprint="corpus-" + digest[-12:],
        digest=digest,
        payload_file="chunks.json.gz",
        payload_bytes=len(canonical_bytes),
        payload_gzip_bytes=len(
            compress(canonical_bytes, compresslevel=6, mtime=0)
        ),
        counts=FrozenInputCounts(
            chunks=counts.chunks,
            text_chunks=counts.text_chunks,
            image_chunks=counts.image_chunks,
            table_chunks=counts.table_chunks,
            chunks_with_metadata=counts.chunks_with_metadata,
            chunks_with_artifact_path=counts.chunks_with_artifact_path,
            map_units=map_units,
            token_rows=token_rows,
        ),
        source_database_identity="db-test-source",
    )


def write_synthetic_frozen_input(
    directory: Path,
    *,
    payload: Mapping[str, Any] | None = None,
    map_units: int = 0,
    token_rows: int = 0,
) -> tuple[dict[str, Any], FrozenInputManifest]:
    """Write a synthetic frozen input that satisfies the harness contract."""
    resolved_payload = dict(payload or build_frozen_payload())
    manifest = build_manifest(
        resolved_payload,
        map_units=map_units,
        token_rows=token_rows,
    )
    write_frozen_input(
        directory,
        payload=resolved_payload,
        manifest=manifest,
        expectations=expectations_for(
            resolved_payload,
            map_units=map_units,
            token_rows=token_rows,
        ),
    )
    return resolved_payload, manifest


def build_clone_record(
    *,
    run_id: str,
    database_url: str,
    profile: str = "production",
    state: str = "ready",
) -> CloneRecord:
    """Build a listed clone record bound to one database URL."""
    return CloneRecord(
        clone_id=run_id,
        run_id=run_id,
        created_at="2026-09-23T00:00:00Z",
        profile=profile,
        postgres_image="postgres:15-alpine",
        postgres_version="15.17",
        schema_revision="20260305_baseline",
        source=SourceVolumeIdentity(
            volume_name=SOURCE_VOLUME_NAME,
            digest="sha256:source-volume-digest",
            container_name=SOURCE_CONTAINER_NAME,
            database_name=SOURCE_DATABASE_NAME,
            database_url_host="127.0.0.1",
            database_url_port=SOURCE_DATABASE_PORT,
        ),
        data_volume=clone_data_volume_name(run_id),
        container_name=clone_container_name(run_id),
        port=55450,
        database_name="knowhere_benchmark",
        database_url_digest=database_url_digest(database_url),
        database_identity={
            "system_identifier": "7000000000000000001",
            "database_name": "knowhere_benchmark",
            "database_oid": "16384",
            "size_bytes": 1024,
        },
        server_settings={"work_mem": "16MB", "shared_buffers": "2GB"},
        index_inventory=(
            {"schema": "public", "table": "documents", "index": "ix_documents"},
        ),
        deviations=(),
        state=state,
    )


def write_listed_clone(
    layout: BenchmarkLayout,
    *,
    run_id: str,
    database_url: str,
    profile: str = "production",
    state: str = "ready",
) -> CloneRecord:
    """Write a listed clone record and its database URL file."""
    record = build_clone_record(
        run_id=run_id,
        database_url=database_url,
        profile=profile,
        state=state,
    )
    layout.ensure_clone_directory(run_id)
    write_clone_record(layout.clone_record_path(run_id), record)
    layout.database_url_path(run_id).write_text(
        database_url + "\n",
        encoding="utf-8",
    )
    return record
