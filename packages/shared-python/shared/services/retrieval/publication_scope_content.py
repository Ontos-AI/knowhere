"""Scope-granular retrieval content replacement for Phase B page-memory enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.chunks.chunk_connections import merge_connections
from shared.services.retrieval.cache_service import (
    bump_retrieval_namespace_cache_version_sync,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.hydration.row_utils import iter_connected_target_ids
from shared.services.retrieval.map_unit_index import replace_document_map_units
from shared.services.retrieval.page_embedding_index import (
    extract_and_strip_page_embeddings,
    inherit_missing_page_assets,
    load_revision_page_assets_by_page,
    replace_document_page_embeddings,
    upsert_document_page_embeddings,
)
from shared.services.retrieval.publication_chunk_copy import insert_chunks_with_copy
from shared.services.retrieval.publication_content import (
    DocumentSectionPublisher,
    _build_document_chunk,
    _get_chunk_metadata,
    _get_source_path,
    deduplicate_chunks_by_source_path,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_trace_stage import trace_publication_stage
from shared.services.retrieval.search.lexical_text import section_path_from_chunk_path
from shared.services.retrieval.serving_manifest import persist_revision_serving_state
from shared.utils.json_utils import remove_nul_characters

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace

_SCOPE_ID_RE = re.compile(r"^p([1-9]\d*)-([1-9]\d*)$")
_REFINED_ENRICHMENT_STATE = "refined"


def replace_document_scope_content(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    scope_id: str,
    chunks: list[dict[str, Any]],
    section_summaries: dict[str, str] | None = None,
    trace: PublicationTrace | None = None,
) -> dict[str, Any]:
    """Replace one scope's sections/chunks, rebuild map units, and bump cache version."""
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    with trace_publication_stage(trace, "chunks_prepare"):
        deduped_chunks = deduplicate_chunks_by_source_path(chunks)
        prior_page_assets_by_page = load_revision_page_assets_by_page(
            db,
            scope=scope,
        )

    with trace_publication_stage(trace, "chunks_persist"):
        if scope_id == "*":
            db.execute(
                delete(corpusStorage.DocumentChunk)
                .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
                .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
            )
        else:
            _delete_or_transfer_scope_chunks(
                db,
                corpus_storage=corpusStorage,
                scope=scope,
                scope_id=scope_id,
            )
        deduped_chunks = _reuse_existing_revision_source_paths(
            db,
            corpus_storage=corpusStorage,
            scope=scope,
            incoming_chunks=deduped_chunks,
        )

    with trace_publication_stage(trace, "sections_persist"):
        _delete_scope_exclusive_leaf_sections(
            db,
            scope=scope,
            incoming_chunks=deduped_chunks,
        )

    section_publisher = DocumentSectionPublisher(
        db=db,
        scope=scope,
        section_summaries=section_summaries,
        preload_existing=True,
    )
    prepared_chunks: list[
        tuple[int, dict[str, Any], dict[str, Any], str | None, DocumentSection]
    ] = []
    incoming_embeddings_by_page: dict[int, list[float]] = {}
    with trace_publication_stage(trace, "sections_prepare"):
        for index, chunk in enumerate(deduped_chunks):
            safe_chunk = cast(dict[str, Any], remove_nul_characters(chunk))
            chunk_metadata = dict(_get_chunk_metadata(safe_chunk))
            incoming_embeddings_by_page.update(
                extract_and_strip_page_embeddings(chunk_metadata)
            )
            if scope_id != "*":
                chunk_metadata["scope_id"] = scope_id
            chunk_type = str(
                safe_chunk.get("type") or safe_chunk.get("chunk_type") or "text"
            )
            if chunk_type == "page":
                if scope_id != "*":
                    chunk_metadata["enrichment_state"] = _REFINED_ENRICHMENT_STATE
                else:
                    chunk_metadata.setdefault(
                        "enrichment_state", _REFINED_ENRICHMENT_STATE
                    )
                inherit_missing_page_assets(
                    chunk_metadata,
                    prior_page_assets_by_page,
                )
            source_path = _get_source_path(
                chunk=safe_chunk,
                chunk_metadata=chunk_metadata,
            )
            section_path = section_path_from_chunk_path(
                source_path,
                source_file_name=scope.source_file_name,
            )
            section = section_publisher.ensure_section(section_path)
            prepared_chunks.append(
                (index, safe_chunk, chunk_metadata, source_path, section)
            )
    with trace_publication_stage(trace, "sections_persist"):
        db.flush()

    chunk_rows: list[DocumentChunk] = []
    with trace_publication_stage(trace, "chunks_prepare"):
        for index, safe_chunk, chunk_metadata, source_path, section in prepared_chunks:
            chunk_rows.append(
                _build_document_chunk(
                    chunk=safe_chunk,
                    chunk_metadata=chunk_metadata,
                    source_path=source_path,
                    section=section,
                    scope=scope,
                    fallback_sort_order=index,
                )
            )
    with trace_publication_stage(trace, "chunks_persist"):
        if chunk_rows:
            insert_chunks_with_copy(db, chunk_rows)
        _normalize_revision_sort_orders(db, scope=scope)

    replace_document_map_units(
        db,
        scope=scope,
        trace=trace,
        clear_existing=True,
    )
    if incoming_embeddings_by_page:
        upsert_document_page_embeddings(
            db, scope=scope, incoming_embeddings_by_page=incoming_embeddings_by_page
        )
    else:
        replace_document_page_embeddings(db, scope=scope)

    manifest_payload = persist_revision_serving_state(db, scope=scope, trace=trace)
    _invalidate_namespace_after_commit(
        db,
        user_id=scope.user_id,
        namespace=scope.namespace,
    )
    return manifest_payload


def _delete_or_transfer_scope_chunks(
    db: Session,
    *,
    corpus_storage: CorpusStorage,
    scope: DocumentPublicationScope,
    scope_id: str,
) -> None:
    """Delete this scope's chunks, transferring shared boundary assets to live peers."""
    scope_chunks = list(
        db.scalars(
            select(corpus_storage.DocumentChunk)
            .where(corpus_storage.DocumentChunk.document_id == scope.document_id)
            .where(corpus_storage.DocumentChunk.job_result_id == scope.job_result_id)
            .where(
                corpus_storage.DocumentChunk.chunk_metadata["scope_id"].as_string()
                == scope_id
            )
        ).all()
    )
    if not scope_chunks:
        return

    scope_assets_by_id = {
        str(chunk.chunk_id): chunk
        for chunk in scope_chunks
        if chunk.chunk_type in ("image", "table") and chunk.chunk_id
    }
    transferred_asset_pks: set[str] = set()
    if scope_assets_by_id:
        peer_chunks = list(
            db.scalars(
                select(corpus_storage.DocumentChunk)
                .where(corpus_storage.DocumentChunk.document_id == scope.document_id)
                .where(corpus_storage.DocumentChunk.job_result_id == scope.job_result_id)
                .where(
                    corpus_storage.DocumentChunk.chunk_metadata["scope_id"].as_string()
                    != scope_id
                )
            ).all()
        )
        for peer in peer_chunks:
            peer_meta = peer.chunk_metadata if isinstance(peer.chunk_metadata, dict) else {}
            peer_scope_id = str(peer_meta.get("scope_id") or "").strip()
            if not peer_scope_id:
                continue
            for target_id in iter_connected_target_ids({"chunk_metadata": peer_meta}):
                asset_row = scope_assets_by_id.get(target_id)
                if asset_row is None or asset_row.id in transferred_asset_pks:
                    continue
                asset_meta = dict(asset_row.chunk_metadata or {})
                asset_meta["scope_id"] = peer_scope_id
                asset_row.chunk_metadata = asset_meta
                transferred_asset_pks.add(str(asset_row.id))

    delete_pks = [
        str(chunk.id)
        for chunk in scope_chunks
        if str(chunk.id) not in transferred_asset_pks
    ]
    if delete_pks:
        db.execute(
            delete(corpus_storage.DocumentChunk)
            .where(corpus_storage.DocumentChunk.document_id == scope.document_id)
            .where(corpus_storage.DocumentChunk.job_result_id == scope.job_result_id)
            .where(corpus_storage.DocumentChunk.id.in_(delete_pks))
        )
    db.flush()


def _reuse_existing_revision_source_paths(
    db: Session,
    *,
    corpus_storage: CorpusStorage,
    scope: DocumentPublicationScope,
    incoming_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse existing peer chunks on overlapping boundary pages instead of colliding."""
    incoming_paths = [
        source_path
        for chunk in incoming_chunks
        if (
            source_path := _get_source_path(
                chunk=chunk,
                chunk_metadata=_get_chunk_metadata(chunk),
            )
        )
    ]
    if not incoming_paths:
        return incoming_chunks

    existing_by_path = {
        str(row.source_chunk_path): row
        for row in db.scalars(
            select(corpus_storage.DocumentChunk)
            .where(corpus_storage.DocumentChunk.document_id == scope.document_id)
            .where(corpus_storage.DocumentChunk.job_result_id == scope.job_result_id)
            .where(corpus_storage.DocumentChunk.source_chunk_path.in_(incoming_paths))
        ).all()
        if row.source_chunk_path
    }
    if not existing_by_path:
        return incoming_chunks

    remapped_ids: dict[str, str] = {}
    incoming_edges_by_existing_id: dict[str, list[Any]] = {}
    retained_chunks: list[dict[str, Any]] = []
    for chunk in incoming_chunks:
        metadata = _get_chunk_metadata(chunk)
        source_path = _get_source_path(chunk=chunk, chunk_metadata=metadata)
        existing = existing_by_path.get(source_path or "")
        if existing is None:
            retained_chunks.append(chunk)
            continue
        incoming_id = str(chunk.get("chunk_id") or "").strip()
        existing_id = str(existing.chunk_id or "").strip()
        if incoming_id and existing_id and incoming_id != existing_id:
            remapped_ids[incoming_id] = existing_id
        incoming_edges = metadata.get("connect_to")
        if existing_id and isinstance(incoming_edges, list) and incoming_edges:
            incoming_edges_by_existing_id.setdefault(existing_id, []).extend(
                incoming_edges
            )

    if remapped_ids:
        for chunk in retained_chunks:
            metadata = _get_chunk_metadata(chunk)
            connections = metadata.get("connect_to")
            if not isinstance(connections, list) or not connections:
                continue
            rewritten: list[Any] = []
            for item in connections:
                if not isinstance(item, dict):
                    rewritten.append(item)
                    continue
                target = str(item.get("target") or "").strip()
                mapped = remapped_ids.get(target)
                rewritten.append({**item, "target": mapped} if mapped else item)
            metadata["connect_to"] = merge_connections(rewritten)

    for row in existing_by_path.values():
        extra_edges = incoming_edges_by_existing_id.get(str(row.chunk_id or ""))
        if not extra_edges:
            continue
        row_meta = dict(row.chunk_metadata or {})
        existing_edges = row_meta.get("connect_to")
        row_meta["connect_to"] = merge_connections(
            existing_edges if isinstance(existing_edges, list) else [],
            extra_edges,
        )
        row.chunk_metadata = row_meta

    return retained_chunks


def _delete_scope_exclusive_leaf_sections(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    incoming_chunks: list[dict[str, Any]],
) -> None:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    other_section_ids = set(
        db.scalars(
            select(corpusStorage.DocumentChunk.section_id)
            .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
            .where(corpusStorage.DocumentChunk.section_id.is_not(None))
            .distinct()
        ).all()
    )
    existing_sections = list(
        db.scalars(
            select(corpusStorage.DocumentSection)
            .where(corpusStorage.DocumentSection.document_id == scope.document_id)
            .where(corpusStorage.DocumentSection.job_result_id == scope.job_result_id)
        ).all()
    )
    if not existing_sections:
        return

    retained_paths: set[str] = set()
    for section in existing_sections:
        if section.section_id in other_section_ids:
            retained_paths.update(_self_and_ancestor_paths(section.section_path))

    for chunk in incoming_chunks:
        safe_chunk = cast(dict[str, Any], remove_nul_characters(chunk))
        chunk_metadata = _get_chunk_metadata(safe_chunk)
        source_path = _get_source_path(
            chunk=safe_chunk,
            chunk_metadata=chunk_metadata,
        )
        section_path = section_path_from_chunk_path(
            source_path,
            source_file_name=scope.source_file_name,
        )
        retained_paths.update(_strict_ancestor_paths(section_path))

    stale_section_ids = [
        section.section_id
        for section in existing_sections
        if section.section_path not in retained_paths
    ]
    if stale_section_ids:
        db.execute(
            delete(corpusStorage.DocumentSection)
            .where(corpusStorage.DocumentSection.document_id == scope.document_id)
            .where(corpusStorage.DocumentSection.job_result_id == scope.job_result_id)
            .where(corpusStorage.DocumentSection.section_id.in_(stale_section_ids))
        )


def _normalize_revision_sort_orders(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> None:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
    chunks = list(
        db.scalars(
            select(corpusStorage.DocumentChunk)
            .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
            .order_by(
                corpusStorage.DocumentChunk.sort_order,
                corpusStorage.DocumentChunk.id,
            )
        ).all()
    )
    if not chunks:
        return

    ordered_chunks = sorted(
        chunks,
        key=lambda row: (
            _scope_sort_key(row.chunk_metadata),
            int(row.sort_order or 0),
            str(row.id or ""),
        ),
    )
    for new_order, chunk in enumerate(ordered_chunks):
        if chunk.sort_order != new_order:
            chunk.sort_order = new_order

    sections = list(
        db.scalars(
            select(corpusStorage.DocumentSection)
            .where(corpusStorage.DocumentSection.document_id == scope.document_id)
            .where(corpusStorage.DocumentSection.job_result_id == scope.job_result_id)
            .order_by(
                corpusStorage.DocumentSection.sort_order,
                corpusStorage.DocumentSection.section_id,
            )
        ).all()
    )
    sections_by_id = {section.section_id: section for section in sections}
    sections_by_path = {section.section_path: section for section in sections}
    visited_paths: list[str] = []
    seen_paths: set[str] = set()
    for chunk in ordered_chunks:
        section = sections_by_id.get(chunk.section_id or "")
        if section is None:
            continue
        for ancestor_path in _self_and_ancestor_paths(section.section_path):
            if ancestor_path not in seen_paths and ancestor_path in sections_by_path:
                seen_paths.add(ancestor_path)
                visited_paths.append(ancestor_path)
    for section in sections:
        if section.section_path not in seen_paths:
            seen_paths.add(section.section_path)
            visited_paths.append(section.section_path)
    for new_order, path in enumerate(visited_paths):
        section = sections_by_path[path]
        if section.sort_order != new_order:
            section.sort_order = new_order

    db.flush()


def _scope_sort_key(metadata: Any) -> tuple[int, int, int]:
    if isinstance(metadata, dict):
        scope_id = str(metadata.get("scope_id") or "").strip()
        matched = _SCOPE_ID_RE.fullmatch(scope_id)
        if matched is not None:
            return (0, int(matched.group(1)), int(matched.group(2)))
    return (1, 0, 0)


def _self_and_ancestor_paths(section_path: str) -> list[str]:
    parts = [part for part in section_path.split(" / ") if part]
    return [" / ".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def _strict_ancestor_paths(section_path: str) -> list[str]:
    parts = [part for part in section_path.split(" / ") if part]
    if len(parts) <= 1:
        return []
    return [" / ".join(parts[:depth]) for depth in range(1, len(parts))]


def _invalidate_namespace_after_commit(
    db: Session, *, user_id: str, namespace: str
) -> None:
    """Invalidate after SQL visibility; a rolled-back publication never advances Redis."""
    is_active: list[bool] = [True]

    def invalidate(session: Session) -> None:
        del session
        if is_active[0]:
            bump_retrieval_namespace_cache_version_sync(
                user_id=user_id, namespace=namespace
            )
        is_active[0] = False

    def cancel(session: Session) -> None:
        del session
        is_active[0] = False

    db.begin() if not db.in_transaction() else None
    event.listen(db, "after_commit", invalidate, once=True)
    event.listen(db, "after_rollback", cancel, once=True)
