from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.search.lexical_text import (
    build_content_lexical_text,
    build_content_search_text,
    build_path_lexical_text,
    build_path_search_text,
    build_term_search_text,
    section_path_from_chunk_path,
)


def replace_document_revision_content(
    db: Session,
    *,
    scope: DocumentPublicationScope,
    chunks: list[dict[str, Any]],
) -> None:
    """Replace retrieval sections and chunks for one published document revision."""
    _delete_existing_revision_content(db, scope=scope)
    section_publisher = DocumentSectionPublisher(db=db, scope=scope)
    for index, chunk in enumerate(chunks):
        chunk_metadata = _get_chunk_metadata(chunk)
        source_path = _get_source_path(chunk=chunk, chunk_metadata=chunk_metadata)
        section_path = section_path_from_chunk_path(
            source_path,
            source_file_name=scope.source_file_name,
        )
        section = section_publisher.ensure_section(section_path)
        db.add(
            _build_document_chunk(
                chunk=chunk,
                chunk_metadata=chunk_metadata,
                source_path=source_path,
                section=section,
                scope=scope,
                fallback_sort_order=index,
            )
        )


class DocumentSectionPublisher:
    def __init__(self, *, db: Session, scope: DocumentPublicationScope) -> None:
        self._db = db
        self._scope = scope
        self._sections_by_path: dict[str, DocumentSection] = {}

    def ensure_section(self, section_path: str) -> DocumentSection:
        existing_section = self._sections_by_path.get(section_path)
        if existing_section is not None:
            return existing_section

        path_parts = [part for part in section_path.split(" / ") if part]
        for depth in range(1, len(path_parts) + 1):
            ancestor_path = " / ".join(path_parts[:depth])
            if ancestor_path in self._sections_by_path:
                continue

            ancestor_section = DocumentSection(
                user_id=self._scope.user_id,
                namespace=self._scope.namespace,
                document_id=self._scope.document_id,
                job_result_id=self._scope.job_result_id,
                parent_section_id=self._get_parent_section_id(path_parts, depth),
                section_path=ancestor_path,
                section_title=path_parts[depth - 1],
                section_level=depth,
                section_metadata={},
                sort_order=len(self._sections_by_path),
            )
            self._db.add(ancestor_section)
            self._db.flush()
            self._sections_by_path[ancestor_path] = ancestor_section

        return self._sections_by_path[section_path]

    def _get_parent_section_id(
        self,
        path_parts: list[str],
        depth: int,
    ) -> str | None:
        if depth <= 1:
            return None
        parent_path = " / ".join(path_parts[: depth - 1])
        parent = self._sections_by_path.get(parent_path)
        return parent.section_id if parent is not None else None


def _delete_existing_revision_content(
    db: Session,
    *,
    scope: DocumentPublicationScope,
) -> None:
    db.execute(
        delete(DocumentChunk)
        .where(DocumentChunk.document_id == scope.document_id)
        .where(DocumentChunk.job_result_id == scope.job_result_id)
    )
    db.execute(
        delete(DocumentSection)
        .where(DocumentSection.document_id == scope.document_id)
        .where(DocumentSection.job_result_id == scope.job_result_id)
    )


def _build_document_chunk(
    *,
    chunk: dict[str, Any],
    chunk_metadata: dict[str, Any],
    source_path: str | None,
    section: DocumentSection,
    scope: DocumentPublicationScope,
    fallback_sort_order: int,
) -> DocumentChunk:
    section_summary = section.summary
    section_path = section.section_path
    section_title = section.section_title
    path_text = f"{scope.source_file_name or ''} {section_path}".strip()
    return DocumentChunk(
        id=f"dchk_{uuid4().hex[:12]}",
        chunk_id=str(chunk.get("chunk_id") or f"chunk_{uuid4().hex[:12]}"),
        user_id=scope.user_id,
        namespace=scope.namespace,
        document_id=scope.document_id,
        job_result_id=scope.job_result_id,
        section_id=section.section_id,
        chunk_type=chunk.get("type") or chunk.get("chunk_type") or "text",
        content=chunk.get("content") or chunk.get("text"),
        content_lexical_text=build_content_lexical_text(chunk),
        path_lexical_text=build_path_lexical_text(
            source_path,
            source_file_name=scope.source_file_name,
        ),
        content_search_text=build_content_search_text(
            chunk,
            section_summary=section_summary,
        ),
        path_search_text=build_path_search_text(
            source_file_name=scope.source_file_name,
            section_path=section_path,
            section_title=section_title,
            section_summary=section_summary,
        ),
        term_search_text=build_term_search_text(chunk, path_text=path_text),
        source_chunk_path=source_path,
        file_path=chunk_metadata.get("file_path") or chunk.get("file_path"),
        chunk_metadata=chunk_metadata,
        sort_order=_get_sort_order(chunk, fallback_sort_order),
    )


def _get_chunk_metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _get_source_path(
    *,
    chunk: dict[str, Any],
    chunk_metadata: dict[str, Any],
) -> str | None:
    source_path = chunk_metadata.get("path") or chunk.get("path")
    return str(source_path) if source_path is not None else None


def _get_sort_order(chunk: dict[str, Any], fallback_sort_order: int) -> int:
    try:
        return int(chunk.get("order", fallback_sort_order))
    except (TypeError, ValueError):
        return fallback_sort_order
