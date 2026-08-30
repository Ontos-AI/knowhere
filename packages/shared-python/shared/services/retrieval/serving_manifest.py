"""Versioned compression and integrity checks for serving manifests."""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentMapUnit,
    DocumentMapUnitToken,
    DocumentSection,
    RetrievalNamespaceGeneration,
    RetrievalNamespaceStat,
    RetrievalNamespaceTokenStat,
    RetrievalServingRevisionManifest,
    RetrievalServingRevisionStat,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.publication_models import DocumentPublicationScope

SERVING_MANIFEST_FORMAT_VERSION = 1


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
    map_units = list(
        db.scalars(
            select(DocumentMapUnit)
            .where(DocumentMapUnit.document_id == scope.document_id)
            .where(DocumentMapUnit.job_result_id == scope.job_result_id)
            .order_by(DocumentMapUnit.sort_order, DocumentMapUnit.unit_id)
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
        "map_units": [
            {
                "row_id": unit.id,
                "unit_id": unit.unit_id,
                "section_id": unit.section_id,
                "unit_kind": unit.unit_kind,
                "path_token_count": unit.path_token_count,
                "content_token_count": unit.content_token_count,
                "sort_order": unit.sort_order,
            }
            for unit in map_units
        ],
        "root_asset_ids": sorted(root_asset_ids),
        "remounted_assets_by_section": remounted_assets,
    }


def build_revision_statistics_payload(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> dict[str, Any]:
    """Build compressed scoring contributions for one revision."""
    units = list(
        db.scalars(
            select(DocumentMapUnit)
            .where(DocumentMapUnit.document_id == scope.document_id)
            .where(DocumentMapUnit.job_result_id == scope.job_result_id)
        )
    )
    unit_ids = [unit.id for unit in units]
    frequencies: dict[str, dict[str, int]] = {"path": {}, "content": {}}
    unit_frequencies: dict[str, dict[str, dict[str, int]]] = {}
    if unit_ids:
        for map_unit_id, channel, token, frequency in db.execute(
            select(
                DocumentMapUnitToken.map_unit_id,
                DocumentMapUnitToken.channel,
                DocumentMapUnitToken.token,
                DocumentMapUnitToken.frequency,
            ).where(DocumentMapUnitToken.map_unit_id.in_(unit_ids))
        ).all():
            channel_key = str(channel)
            if channel_key in frequencies:
                token_key = str(token)
                frequency_value = int(frequency)
                frequencies[channel_key][token_key] = (
                    frequencies[channel_key].get(token_key, 0) + frequency_value
                )
                unit_frequencies.setdefault(str(map_unit_id), {}).setdefault(
                    channel_key, {}
                )[token_key] = frequency_value
    return {
        "document_id": scope.document_id,
        "job_result_id": scope.job_result_id,
        "unit_count": len(units),
        "path_token_count": sum(int(unit.path_token_count or 0) for unit in units),
        "content_token_count": sum(
            int(unit.content_token_count or 0) for unit in units
        ),
        "token_frequencies": frequencies,
        "unit_frequencies": unit_frequencies,
    }


def persist_revision_serving_state(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> None:
    """Replace manifest and statistics rows for one revision atomically."""
    manifest_payload = build_revision_serving_payload(db, scope=scope)
    statistics_payload = build_revision_statistics_payload(db, scope=scope)
    manifest_bytes, manifest_checksum, manifest_version = encode_serving_manifest(
        manifest_payload
    )
    statistics_bytes, statistics_checksum, statistics_version = encode_serving_manifest(
        statistics_payload
    )
    db.execute(
        delete(RetrievalServingRevisionManifest)
        .where(RetrievalServingRevisionManifest.document_id == scope.document_id)
        .where(RetrievalServingRevisionManifest.job_result_id == scope.job_result_id)
    )
    db.execute(
        delete(RetrievalServingRevisionStat)
        .where(RetrievalServingRevisionStat.document_id == scope.document_id)
        .where(RetrievalServingRevisionStat.job_result_id == scope.job_result_id)
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
    db.add(
        RetrievalServingRevisionStat(
            user_id=scope.user_id,
            namespace=scope.namespace,
            document_id=scope.document_id,
            job_result_id=scope.job_result_id,
            format_version=statistics_version,
            payload_zlib=statistics_bytes,
            checksum=statistics_checksum,
        )
    )


def rebuild_namespace_serving_statistics(
    db: Session,
    *,
    user_id: str,
    namespace: str,
) -> int:
    """Recompute namespace aggregates from active current revisions.

    Callers hold the namespace generation lock. The aggregate is prepared for
    the generation that the caller will publish next.
    """
    generation = db.execute(
        select(RetrievalNamespaceGeneration)
        .where(RetrievalNamespaceGeneration.user_id == user_id)
        .where(RetrievalNamespaceGeneration.namespace == namespace)
        .with_for_update()
    ).scalar_one()
    target_generation = int(generation.generation) + 1
    revisions = {
        (str(document_id), str(job_result_id))
        for document_id, job_result_id in db.execute(
            select(Document.document_id, Document.current_job_result_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == "active")
            .where(Document.current_job_result_id.is_not(None))
        ).all()
        if document_id and job_result_id
    }
    aggregate: dict[str, Any] = {
        "document_count": 0,
        "unit_count": 0,
        "path_token_count": 0,
        "content_token_count": 0,
        "token_frequencies": {"path": {}, "content": {}},
    }
    document_frequencies: dict[tuple[str, str], int] = {}
    for row in db.scalars(
        select(RetrievalServingRevisionStat)
        .where(RetrievalServingRevisionStat.user_id == user_id)
        .where(RetrievalServingRevisionStat.namespace == namespace)
    ):
        if (row.document_id, row.job_result_id) not in revisions:
            continue
        payload = decode_serving_manifest(
            row.payload_zlib,
            checksum=row.checksum,
            format_version=row.format_version,
        )
        aggregate["document_count"] += 1
        aggregate["unit_count"] += int(payload.get("unit_count", 0))
        aggregate["path_token_count"] += int(payload.get("path_token_count", 0))
        aggregate["content_token_count"] += int(payload.get("content_token_count", 0))
        token_frequencies = payload.get("token_frequencies", {})
        if not isinstance(token_frequencies, dict):
            continue
        for channel, values in token_frequencies.items():
            if channel not in aggregate["token_frequencies"] or not isinstance(
                values, dict
            ):
                continue
            for token, value in values.items():
                token_key = str(token)
                aggregate["token_frequencies"][channel][token_key] = aggregate[
                    "token_frequencies"
                ][channel].get(token_key, 0) + int(value)
                if int(value) > 0:
                    key = (str(channel), token_key)
                    document_frequencies[key] = document_frequencies.get(key, 0) + 1

    encoded, checksum, _version = encode_serving_manifest(aggregate)
    namespace_stat = db.execute(
        select(RetrievalNamespaceStat)
        .where(RetrievalNamespaceStat.user_id == user_id)
        .where(RetrievalNamespaceStat.namespace == namespace)
    ).scalar_one_or_none()
    if namespace_stat is None:
        db.add(
            RetrievalNamespaceStat(
                user_id=user_id,
                namespace=namespace,
                generation=target_generation,
                payload_zlib=encoded,
                checksum=checksum,
            )
        )
    else:
        namespace_stat.generation = target_generation
        namespace_stat.payload_zlib = encoded
        namespace_stat.checksum = checksum
    db.execute(
        delete(RetrievalNamespaceTokenStat)
        .where(RetrievalNamespaceTokenStat.user_id == user_id)
        .where(RetrievalNamespaceTokenStat.namespace == namespace)
    )
    db.add_all(
        [
            RetrievalNamespaceTokenStat(
                user_id=user_id,
                namespace=namespace,
                generation=target_generation,
                channel=channel,
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                document_frequency=frequency,
            )
            for (channel, token), frequency in document_frequencies.items()
        ]
    )
    db.flush()
    return target_generation


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
) -> dict[str, Any]:
    """Validate and decode one persisted serving manifest."""
    if format_version != SERVING_MANIFEST_FORMAT_VERSION:
        raise ValueError(f"unsupported serving manifest version: {format_version}")

    try:
        canonical_payload = zlib.decompress(payload_zlib)
    except zlib.error as exc:
        raise ValueError("invalid serving manifest compression") from exc

    actual_checksum = hashlib.sha256(canonical_payload).hexdigest()
    if actual_checksum != checksum:
        raise ValueError("serving manifest checksum mismatch")

    try:
        decoded = json.loads(canonical_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid serving manifest JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("serving manifest payload must be an object")
    return decoded
