"""Async namespace snapshot loader for map-nav.

Loads current-revision sections + chunks for ``(user_id, namespace)``, builds a
synchronous ``NamespaceKnowhereProvider``, and a separate ``chunk_ref_index``
that keeps **DB-original** ``section_path`` / ``job_id`` (never remounted).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    NamespaceKnowhereProvider,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.search.section_filters import is_excluded_section


@dataclass(frozen=True)
class NavSnapshot:
    """In-memory corpus for one map-nav episode."""

    provider: NamespaceKnowhereProvider
    chunk_ref_index: dict[str, dict[str, Any]]
    document_ids: list[str]
    document_titles: dict[str, str]


def build_nav_snapshot(
    *,
    document_titles: dict[str, str],
    sections_by_doc: dict[str, list[SectionRow]],
    units_by_doc: dict[str, list[UnitRow]],
    chunk_ref_index: dict[str, dict[str, Any]],
) -> NavSnapshot:
    """Assemble provider + index from already-fetched rows (also used by tests)."""
    doc_ids = [did for did in document_titles if did in sections_by_doc or did in units_by_doc]
    if not doc_ids:
        raise ValueError("nav snapshot requires at least one active document")

    providers: list[KnowhereProvider] = []
    for doc_id in doc_ids:
        providers.append(
            KnowhereProvider(
                doc_id=doc_id,
                sections=sections_by_doc.get(doc_id, ()),
                units=units_by_doc.get(doc_id, ()),
            )
        )
    provider = NamespaceKnowhereProvider(providers, titles=document_titles)
    return NavSnapshot(
        provider=provider,
        chunk_ref_index=dict(chunk_ref_index),
        document_ids=list(provider.document_ids()),
        document_titles={did: document_titles.get(did, did) for did in provider.document_ids()},
    )


async def load_nav_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str] | None = None,
    exclude_sections: list[dict[str, str]] | None = None,
) -> NavSnapshot:
    """Preload namespace current revision into a sync map-nav snapshot."""
    excluded_docs = [str(x).strip() for x in (exclude_document_ids or ()) if str(x).strip()]
    excluded_secs = list(exclude_sections or ())

    doc_stmt = (
        select(Document.document_id, Document.source_file_name, Document.current_job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .where(Document.current_job_result_id.is_not(None))
        .order_by(Document.document_id)
    )
    if excluded_docs:
        doc_stmt = doc_stmt.where(Document.document_id.notin_(excluded_docs))
    doc_rows = list((await db.execute(doc_stmt)).all())
    if not doc_rows:
        raise ValueError(
            f"no active documents with current revision for "
            f"user_id={user_id!r} namespace={namespace!r}"
        )

    document_titles: dict[str, str] = {}
    for document_id, source_file_name, _job in doc_rows:
        did = str(document_id)
        title = str(source_file_name or "").strip() or did
        document_titles[did] = title

    sections_by_doc, section_path_by_id = await _load_sections(
        db,
        user_id=user_id,
        namespace=namespace,
        exclude_document_ids=excluded_docs,
        exclude_sections=excluded_secs,
    )
    units_by_doc, chunk_ref_index = await _load_chunks(
        db,
        user_id=user_id,
        namespace=namespace,
        exclude_document_ids=excluded_docs,
        exclude_sections=excluded_secs,
        section_path_by_id=section_path_by_id,
    )

    # Keep only documents that still have sections after exclude filters.
    kept_titles = {
        did: title
        for did, title in document_titles.items()
        if sections_by_doc.get(did)
    }
    if not kept_titles:
        raise ValueError(
            f"nav snapshot empty after excludes for "
            f"user_id={user_id!r} namespace={namespace!r}"
        )

    return build_nav_snapshot(
        document_titles=kept_titles,
        sections_by_doc={did: sections_by_doc.get(did, []) for did in kept_titles},
        units_by_doc={did: units_by_doc.get(did, []) for did in kept_titles},
        chunk_ref_index=chunk_ref_index,
    )


async def _load_sections(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> tuple[dict[str, list[SectionRow]], dict[str, str]]:
    stmt = (
        select(
            Document.document_id,
            DocumentSection.section_id,
            DocumentSection.parent_section_id,
            DocumentSection.section_path,
            DocumentSection.section_title,
            DocumentSection.section_level,
            DocumentSection.summary,
            DocumentSection.sort_order,
        )
        .join(
            DocumentSection,
            (DocumentSection.document_id == Document.document_id)
            & (DocumentSection.job_result_id == Document.current_job_result_id),
        )
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .order_by(Document.document_id, DocumentSection.sort_order, DocumentSection.section_id)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))

    by_doc: dict[str, list[SectionRow]] = {}
    path_by_id: dict[str, str] = {}
    for row in (await db.execute(stmt)).all():
        document_id = str(row[0])
        section_path = str(row[3] or "")
        if is_excluded_section(
            document_id=document_id,
            section_path=section_path,
            exclude_sections=exclude_sections,
        ):
            continue
        section_id = str(row[1])
        section = SectionRow(
            section_id=section_id,
            parent_section_id=str(row[2]) if row[2] else None,
            section_path=section_path,
            section_title=str(row[4] or "").strip(),
            section_level=int(row[5] or 0),
            summary=str(row[6] or "").strip(),
            sort_order=int(row[7] or 0),
        )
        by_doc.setdefault(document_id, []).append(section)
        path_by_id[section_id] = section_path
    return by_doc, path_by_id


async def _load_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    section_path_by_id: dict[str, str],
) -> tuple[dict[str, list[UnitRow]], dict[str, dict[str, Any]]]:
    stmt = (
        select(
            Document.document_id,
            DocumentChunk.chunk_id,
            DocumentChunk.section_id,
            DocumentChunk.chunk_type,
            DocumentChunk.content,
            DocumentChunk.sort_order,
            DocumentChunk.source_chunk_path,
            DocumentChunk.file_path,
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
            JobResult.job_id,
        )
        .join(
            DocumentChunk,
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id),
        )
        .outerjoin(
            DocumentSection,
            DocumentSection.section_id == DocumentChunk.section_id,
        )
        .outerjoin(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .order_by(Document.document_id, DocumentChunk.sort_order, DocumentChunk.chunk_id)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))

    by_doc: dict[str, list[UnitRow]] = {}
    ref_index: dict[str, dict[str, Any]] = {}
    for row in (await db.execute(stmt)).all():
        document_id = str(row[0])
        chunk_id = str(row[1] or "").strip()
        section_id = str(row[2]) if row[2] else None
        # Prefer joined section_path; fall back to kept section map.
        section_path: Optional[str] = str(row[9]) if row[9] is not None else None
        if section_path is None and section_id:
            section_path = section_path_by_id.get(section_id)
        if is_excluded_section(
            document_id=document_id,
            section_path=section_path,
            exclude_sections=exclude_sections,
        ):
            continue
        # Drop units whose section was filtered out of the tree.
        if section_id and section_id not in section_path_by_id:
            continue

        unit = UnitRow(
            chunk_id=chunk_id,
            section_id=section_id,
            chunk_type=str(row[3] or "text"),
            content=str(row[4] or ""),
            sort_order=int(row[5] or 0),
            source_chunk_path=str(row[6] or ""),
            file_path=str(row[7] or ""),
            metadata=_as_meta(row[8]),
        )
        by_doc.setdefault(document_id, []).append(unit)
        if chunk_id:
            meta = {
                "document_id": document_id,
                "section_path": section_path,
                "chunk_type": unit.chunk_type,
                "file_path": unit.file_path or None,
                "job_id": str(row[10]) if row[10] else None,
            }
            # Bare chunk_id (last-wins) plus doc-scoped key so the same
            # chunk_id can appear under multiple documents.
            ref_index[chunk_id] = meta
            ref_index[f"{document_id}:{chunk_id}"] = meta
    return by_doc, ref_index


def _as_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}
