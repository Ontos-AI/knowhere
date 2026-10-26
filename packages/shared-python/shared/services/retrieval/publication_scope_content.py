"""Scope-granular retrieval content replacement for Phase B page-memory enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.cache_service import (
    bump_retrieval_namespace_cache_version_sync,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.map_unit_index import replace_document_map_units
from shared.services.retrieval.page_embedding_index import (
    extract_and_strip_page_embeddings,
    inherit_missing_page_assets,
    load_revision_page_assets_by_page,
    replace_document_page_embeddings,
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
        delete_stmt = (
            delete(corpusStorage.DocumentChunk)
            .where(corpusStorage.DocumentChunk.document_id == scope.document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == scope.job_result_id)
        )
        if scope_id != "*":
            delete_stmt = delete_stmt.where(
                corpusStorage.DocumentChunk.chunk_metadata["scope_id"].as_string()
                == scope_id
            )
        db.execute(delete_stmt)

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
    replace_document_page_embeddings(
        db,
        scope=scope,
        incoming_embeddings_by_page=incoming_embeddings_by_page or None,
    )
    manifest_payload = persist_revision_serving_state(db, scope=scope, trace=trace)
    bump_retrieval_namespace_cache_version_sync(
        user_id=scope.user_id,
        namespace=scope.namespace,
    )
    return manifest_payload


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
