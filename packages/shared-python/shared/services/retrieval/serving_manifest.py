"""Versioned compression and integrity checks for serving manifests."""

from __future__ import annotations

import hashlib
import json
import time
import zlib
from typing import Any, MutableMapping

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
    RetrievalServingRevisionManifest,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.publication_models import DocumentPublicationScope

SERVING_MANIFEST_FORMAT_VERSION = 1
NAMESPACE_MAP_SNAPSHOT_FORMAT_VERSION = 2


def build_revision_serving_payload(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> dict[str, Any]:
    """Build ordered metadata for one published document revision."""
    document = db.execute(
        select(Document).where(Document.document_id == scope.document_id)
    ).scalar_one()
    job_result = db.execute(
        select(JobResult).where(JobResult.id == scope.job_result_id)
    ).scalar_one()
    sections = list(
        db.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_id == scope.document_id)
            .where(DocumentSection.job_result_id == scope.job_result_id)
            .order_by(DocumentSection.sort_order, DocumentSection.section_id)
        )
    )
    chunks = list(
        db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == scope.document_id)
            .where(DocumentChunk.job_result_id == scope.job_result_id)
            .order_by(
                DocumentChunk.sort_order, DocumentChunk.chunk_id, DocumentChunk.id
            )
        )
    )
    section_path_by_id = {
        section.section_id: section.section_path for section in sections
    }
    root_asset_ids = {
        chunk.chunk_id
        for chunk in chunks
        if chunk.chunk_type in {"image", "table"}
        and chunk.section_id is not None
        and section_path_by_id.get(chunk.section_id) == "Root"
    }
    remounted_assets: dict[str, list[str]] = {}
    for chunk in chunks:
        if chunk.chunk_type != "text" or not isinstance(chunk.chunk_metadata, dict):
            continue
        connections = chunk.chunk_metadata.get("connect_to")
        if not isinstance(connections, list):
            continue
        targets = [
            str(connection.get("target") or "").strip()
            for connection in connections
            if isinstance(connection, dict)
            and str(connection.get("target") or "").strip() in root_asset_ids
        ]
        if targets:
            remounted_assets[chunk.section_id or ""] = targets

    return {
        "document_id": scope.document_id,
        "job_result_id": scope.job_result_id,
        "job_id": str(job_result.job_id),
        "source_file_name": str(
            document.source_file_name or scope.source_file_name or ""
        ),
        "sections": [
            {
                "section_id": section.section_id,
                "parent_section_id": section.parent_section_id,
                "section_path": section.section_path,
                "section_title": section.section_title,
                "section_level": section.section_level,
                "summary": section.summary,
                "sort_order": section.sort_order,
            }
            for section in sections
        ],
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "section_id": chunk.section_id,
                "chunk_type": chunk.chunk_type,
                "sort_order": chunk.sort_order,
                "connect_to": _connection_target_ids(chunk.chunk_metadata),
            }
            for chunk in chunks
        ],
        "root_asset_ids": sorted(root_asset_ids),
        "remounted_assets_by_section": remounted_assets,
    }


def persist_revision_serving_state(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> dict[str, Any]:
    """Replace the serving manifest row for one revision atomically.

    Returns the manifest payload so callers can patch the namespace-level MAP
    snapshot without rebuilding it.
    """
    manifest_payload = build_revision_serving_payload(db, scope=scope)
    manifest_bytes, manifest_checksum, manifest_version = encode_serving_manifest(
        manifest_payload
    )
    db.execute(
        delete(RetrievalServingRevisionManifest)
        .where(RetrievalServingRevisionManifest.document_id == scope.document_id)
        .where(RetrievalServingRevisionManifest.job_result_id == scope.job_result_id)
    )
    db.add(
        RetrievalServingRevisionManifest(
            user_id=scope.user_id,
            namespace=scope.namespace,
            document_id=scope.document_id,
            job_result_id=scope.job_result_id,
            format_version=manifest_version,
            payload_zlib=manifest_bytes,
            checksum=manifest_checksum,
        )
    )
    return manifest_payload


def _connection_target_ids(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    connections = metadata.get("connect_to")
    if not isinstance(connections, list):
        return []
    return [
        target
        for connection in connections
        if isinstance(connection, dict)
        for target in [str(connection.get("target") or "").strip()]
        if target
    ]


def encode_serving_manifest(payload: dict[str, Any]) -> tuple[bytes, str, int]:
    """Return compressed canonical JSON, checksum, and format version."""
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    checksum = hashlib.sha256(canonical_payload).hexdigest()
    return (
        zlib.compress(canonical_payload),
        checksum,
        SERVING_MANIFEST_FORMAT_VERSION,
    )


def decode_serving_manifest(
    payload_zlib: bytes,
    *,
    checksum: str,
    format_version: int,
    timings: MutableMapping[str, float] | None = None,
) -> dict[str, Any]:
    """Validate and decode one persisted serving manifest."""
    if format_version != SERVING_MANIFEST_FORMAT_VERSION:
        raise ValueError(f"unsupported serving manifest version: {format_version}")

    return _decode_compressed_json(
        payload_zlib,
        checksum=checksum,
        timings=timings,
        compression_error="invalid serving manifest compression",
        checksum_error="serving manifest checksum mismatch",
        json_error="invalid serving manifest JSON",
        object_error="serving manifest payload must be an object",
    )


def _decode_compressed_json(
    payload_zlib: bytes,
    *,
    checksum: str,
    timings: MutableMapping[str, float] | None,
    compression_error: str,
    checksum_error: str,
    json_error: str,
    object_error: str,
) -> dict[str, Any]:
    """Decompress, validate, and decode a canonical JSON payload."""

    started = time.perf_counter()
    try:
        canonical_payload = zlib.decompress(payload_zlib)
    except zlib.error as exc:
        raise ValueError(compression_error) from exc
    if timings is not None:
        timings["decompress_seconds"] = time.perf_counter() - started

    checksum_started = time.perf_counter()
    actual_checksum = hashlib.sha256(canonical_payload).hexdigest()
    if actual_checksum != checksum:
        raise ValueError(checksum_error)
    if timings is not None:
        timings["checksum_seconds"] = time.perf_counter() - checksum_started

    json_started = time.perf_counter()
    try:
        decoded = json.loads(canonical_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(json_error) from exc
    if timings is not None:
        timings["json_decode_seconds"] = time.perf_counter() - json_started
        timings["compressed_bytes"] = float(len(payload_zlib))
        timings["decompressed_bytes"] = float(len(canonical_payload))
        timings["decode_seconds"] = time.perf_counter() - started
    if not isinstance(decoded, dict):
        raise ValueError(object_error)
    return decoded


def encode_namespace_map_snapshot(
    payload: dict[str, Any],
) -> tuple[bytes, str, int]:
    """Encode the routing-only namespace snapshot using its own format version."""
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("namespace snapshot documents must be an object")
    routing_documents: dict[str, Any] = {}
    for document_id, raw_document in documents.items():
        if not isinstance(raw_document, dict):
            raise ValueError(f"namespace snapshot document is not an object: {document_id}")
        raw_sections = raw_document.get("sections")
        raw_chunks = raw_document.get("chunks")
        if not isinstance(raw_sections, list) or not isinstance(raw_chunks, list):
            raise ValueError(f"namespace snapshot records are invalid: {document_id}")
        sections = []
        for section in raw_sections:
            if not isinstance(section, dict) or not str(section.get("section_id") or ""):
                raise ValueError(f"namespace snapshot section is invalid: {document_id}")
            sections.append(
                {
                    key: section[key]
                    for key in (
                        "section_id", "parent_section_id", "section_path",
                        "section_title", "section_level", "summary", "sort_order",
                    )
                    if key in section
                }
            )
        chunks = []
        for chunk in raw_chunks:
            if not isinstance(chunk, dict) or not str(chunk.get("chunk_id") or ""):
                raise ValueError(f"namespace snapshot chunk is invalid: {document_id}")
            chunks.append(
                {
                    key: chunk[key]
                    for key in ("chunk_id", "section_id", "chunk_type", "sort_order", "connect_to")
                    if key in chunk
                }
            )
        routing_documents[str(document_id)] = {
            "job_result_id": raw_document.get("job_result_id"),
            "job_id": raw_document.get("job_id"),
            "sections": sections,
            "chunks": chunks,
            "root_asset_ids": raw_document.get("root_asset_ids") or [],
            "remounted_assets_by_section": raw_document.get(
                "remounted_assets_by_section"
            )
            or {},
        }
    compressed, checksum, _ = encode_serving_manifest({"documents": routing_documents})
    return compressed, checksum, NAMESPACE_MAP_SNAPSHOT_FORMAT_VERSION


def decode_namespace_map_snapshot(
    payload_zlib: bytes,
    *,
    checksum: str,
    format_version: int,
    timings: MutableMapping[str, float] | None = None,
) -> dict[str, Any]:
    """Decode namespace snapshots, retaining compatibility with legacy v1 rows."""
    if format_version == SERVING_MANIFEST_FORMAT_VERSION:
        return decode_serving_manifest(
            payload_zlib,
            checksum=checksum,
            format_version=SERVING_MANIFEST_FORMAT_VERSION,
            timings=timings,
        )
    if format_version != NAMESPACE_MAP_SNAPSHOT_FORMAT_VERSION:
        raise ValueError(f"unsupported namespace snapshot version: {format_version}")
    return _decode_compressed_json(
        payload_zlib,
        checksum=checksum,
        timings=timings,
        compression_error="invalid namespace snapshot compression",
        checksum_error="namespace snapshot checksum mismatch",
        json_error="invalid namespace snapshot JSON",
        object_error="namespace snapshot payload must be an object",
    )
