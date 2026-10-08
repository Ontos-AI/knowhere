"""Incrementally-patched namespace-level MAP snapshot (sections + chunk index).

Callers must already hold the namespace generation lock (see
``serving_generation.lock_namespace_generation``) before calling either
function here. Each call only touches one document's subtree; every other
document's subtree in the payload is left byte-for-byte unchanged.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.models.database.document import (
    RetrievalNamespaceGeneration,
    RetrievalNamespaceMapSnapshot,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_trace_stage import trace_publication_stage
from shared.services.retrieval.serving_manifest import (
    decode_namespace_map_snapshot,
    encode_namespace_map_snapshot,
)

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


def patch_namespace_map_snapshot(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    manifest_payload: dict[str, Any],
    current_generation: RetrievalNamespaceGeneration | None = None,
    trace: PublicationTrace | None = None,
) -> None:
    """Replace one document's subtree in the namespace MAP snapshot."""
    with trace_publication_stage(trace, "namespace_snapshot_prepare"):
        row = _load_snapshot_row(db, user_id=scope.user_id, namespace=scope.namespace)
        if _should_skip_large_snapshot(row, trace=trace):
            return
        documents = _decode_documents(row)
        documents[scope.document_id] = {
            "job_result_id": manifest_payload.get("job_result_id"),
            "job_id": manifest_payload.get("job_id"),
            "source_file_name": manifest_payload.get("source_file_name"),
            "sections": manifest_payload.get("sections") or [],
            "chunks": manifest_payload.get("chunks") or [],
        }
        target_generation = _target_generation(
            db,
            user_id=scope.user_id,
            namespace=scope.namespace,
            current_generation=current_generation,
        )
        byte_counts: dict[str, int] | None = {} if trace is not None else None
        encoded, checksum, format_version = encode_namespace_map_snapshot(
            {"documents": documents},
            byte_counts=byte_counts,
            assume_canonical=True,
        )
    if trace is not None and byte_counts is not None:
        trace.record_count("namespace_active_documents", len(documents))
        trace.record_count("snapshot_compressed_bytes", byte_counts["compressed"])
        trace.record_count("snapshot_uncompressed_bytes", byte_counts["uncompressed"])
    with trace_publication_stage(trace, "namespace_snapshot_persist"):
        _write_snapshot(
            db,
            row=row,
            user_id=scope.user_id,
            namespace=scope.namespace,
            encoded=encoded,
            checksum=checksum,
            format_version=format_version,
            target_generation=target_generation,
        )
        db.flush()


def remove_document_from_namespace_map_snapshot(
    db: Session,
    *,
    user_id: str,
    namespace: str,
    document_id: str,
    current_generation: RetrievalNamespaceGeneration | None = None,
    trace: PublicationTrace | None = None,
) -> None:
    """Drop one document's subtree from the namespace MAP snapshot (archive path)."""
    with trace_publication_stage(trace, "namespace_snapshot_prepare"):
        row = _load_snapshot_row(db, user_id=user_id, namespace=namespace)
        if _should_skip_large_snapshot(row, trace=trace):
            return
        documents = _decode_documents(row)
        if document_id not in documents:
            return
        del documents[document_id]
        target_generation = _target_generation(
            db,
            user_id=user_id,
            namespace=namespace,
            current_generation=current_generation,
        )
        byte_counts: dict[str, int] | None = {} if trace is not None else None
        encoded, checksum, format_version = encode_namespace_map_snapshot(
            {"documents": documents},
            byte_counts=byte_counts,
            assume_canonical=True,
        )
    if trace is not None and byte_counts is not None:
        trace.record_count("namespace_active_documents", len(documents))
        trace.record_count("snapshot_compressed_bytes", byte_counts["compressed"])
        trace.record_count("snapshot_uncompressed_bytes", byte_counts["uncompressed"])
    with trace_publication_stage(trace, "namespace_snapshot_persist"):
        _write_snapshot(
            db,
            row=row,
            user_id=user_id,
            namespace=namespace,
            encoded=encoded,
            checksum=checksum,
            format_version=format_version,
            target_generation=target_generation,
        )
        db.flush()


def _target_generation(
    db: Session,
    *,
    user_id: str,
    namespace: str,
    current_generation: RetrievalNamespaceGeneration | None = None,
) -> int:
    """Namespace generation this snapshot is prepared for (current + 1).

    Callers advance the generation after this write, in the same transaction.
    """
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    generation = current_generation
    if generation is None:
        generation = db.execute(
            select(corpusStorage.RetrievalNamespaceGeneration)
            .where(corpusStorage.RetrievalNamespaceGeneration.user_id == corpusStorage.resolve_owner(user_id))
            .where(corpusStorage.RetrievalNamespaceGeneration.namespace == namespace)
            .with_for_update()
        ).scalar_one()
    return int(generation.generation) + 1


def _load_snapshot_row(
    db: Session, *, user_id: str, namespace: str
) -> RetrievalNamespaceMapSnapshot | None:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    return db.execute(
        select(corpusStorage.RetrievalNamespaceMapSnapshot)
        .where(corpusStorage.RetrievalNamespaceMapSnapshot.user_id == corpusStorage.resolve_owner(user_id))
        .where(corpusStorage.RetrievalNamespaceMapSnapshot.namespace == namespace)
    ).scalar_one_or_none()


def _should_skip_large_snapshot(
    row: RetrievalNamespaceMapSnapshot | None,
    *,
    trace: PublicationTrace | None,
) -> bool:
    """Avoid rewriting oversized snapshots that already have a fallback path."""
    max_bytes = int(settings.KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES)
    if row is None or max_bytes <= 0 or len(row.payload_zlib) <= max_bytes:
        return False
    if trace is not None:
        trace.record_count("namespace_snapshot_skipped", 1)
    return True


def _decode_documents(
    row: RetrievalNamespaceMapSnapshot | None,
) -> dict[str, dict[str, Any]]:
    if row is None:
        return {}
    try:
        payload = decode_namespace_map_snapshot(
            row.payload_zlib,
            checksum=row.checksum,
            format_version=row.format_version,
        )
    except ValueError:
        return {}
    documents = payload.get("documents")
    return dict(documents) if isinstance(documents, dict) else {}


def _write_snapshot(
    db: Session,
    *,
    row: RetrievalNamespaceMapSnapshot | None,
    user_id: str,
    namespace: str,
    encoded: bytes,
    checksum: str,
    format_version: int,
    target_generation: int,
) -> None:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    if row is None:
        db.add(
            corpusStorage.RetrievalNamespaceMapSnapshot(
                user_id=corpusStorage.resolve_owner(user_id),
                namespace=namespace,
                generation=target_generation,
                format_version=format_version,
                payload_zlib=encoded,
                checksum=checksum,
            )
        )
    else:
        row.generation = target_generation
        row.format_version = format_version
        row.payload_zlib = encoded
        row.checksum = checksum
