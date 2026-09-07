"""Incrementally-patched namespace-level MAP snapshot (sections + chunk index).

Callers must already hold the namespace generation lock (see
``serving_generation.lock_namespace_generation``) before calling either
function here. Each call only touches one document's subtree; every other
document's subtree in the payload is left byte-for-byte unchanged.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.document import (
    RetrievalNamespaceGeneration,
    RetrievalNamespaceMapSnapshot,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.serving_manifest import (
    decode_namespace_map_snapshot,
    encode_namespace_map_snapshot,
)


def patch_namespace_map_snapshot(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    manifest_payload: dict[str, Any],
) -> None:
    """Replace one document's subtree in the namespace MAP snapshot."""
    row = _load_snapshot_row(db, user_id=scope.user_id, namespace=scope.namespace)
    documents = _decode_documents(row)
    documents[scope.document_id] = {
        "job_result_id": manifest_payload.get("job_result_id"),
        "job_id": manifest_payload.get("job_id"),
        "source_file_name": manifest_payload.get("source_file_name"),
        "sections": manifest_payload.get("sections") or [],
        "chunks": manifest_payload.get("chunks") or [],
    }
    target_generation = _target_generation(
        db, user_id=scope.user_id, namespace=scope.namespace
    )
    _write_snapshot(
        db,
        row=row,
        user_id=scope.user_id,
        namespace=scope.namespace,
        documents=documents,
        target_generation=target_generation,
    )


def remove_document_from_namespace_map_snapshot(
    db: Session,
    *,
    user_id: str,
    namespace: str,
    document_id: str,
) -> None:
    """Drop one document's subtree from the namespace MAP snapshot (archive path)."""
    row = _load_snapshot_row(db, user_id=user_id, namespace=namespace)
    documents = _decode_documents(row)
    if document_id not in documents:
        return
    del documents[document_id]
    target_generation = _target_generation(db, user_id=user_id, namespace=namespace)
    _write_snapshot(
        db,
        row=row,
        user_id=user_id,
        namespace=namespace,
        documents=documents,
        target_generation=target_generation,
    )


def _target_generation(db: Session, *, user_id: str, namespace: str) -> int:
    """Namespace generation this snapshot is prepared for (current + 1).

    Callers advance the generation after this write, in the same transaction.
    """
    generation = db.execute(
        select(RetrievalNamespaceGeneration)
        .where(RetrievalNamespaceGeneration.user_id == user_id)
        .where(RetrievalNamespaceGeneration.namespace == namespace)
        .with_for_update()
    ).scalar_one()
    return int(generation.generation) + 1


def _load_snapshot_row(
    db: Session, *, user_id: str, namespace: str
) -> RetrievalNamespaceMapSnapshot | None:
    return db.execute(
        select(RetrievalNamespaceMapSnapshot)
        .where(RetrievalNamespaceMapSnapshot.user_id == user_id)
        .where(RetrievalNamespaceMapSnapshot.namespace == namespace)
    ).scalar_one_or_none()


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
    documents: dict[str, dict[str, Any]],
    target_generation: int,
) -> None:
    encoded, checksum, format_version = encode_namespace_map_snapshot(
        {"documents": documents}
    )
    if row is None:
        db.add(
            RetrievalNamespaceMapSnapshot(
                user_id=user_id,
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
