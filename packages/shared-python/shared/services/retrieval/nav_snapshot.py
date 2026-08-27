"""Async namespace snapshot loader for map-nav.

Loads current-revision sections + chunks for ``(user_id, namespace)``, builds a
synchronous ``NamespaceKnowhereProvider``, and a separate ``chunk_ref_index``
that keeps **DB-original** ``section_path`` / ``job_id`` (never remounted).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import literal, select, tuple_
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


# Keep each payload query bounded under the API's 30-second statement timeout.
# Keyset pagination avoids the increasingly expensive OFFSET scans.
_CHUNK_BATCH_SIZE = 2_000


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
    current_job_result_ids: set[str] = set()
    document_revisions: list[tuple[str, str]] = []
    for document_id, source_file_name, current_job_result_id in doc_rows:
        did = str(document_id)
        title = str(source_file_name or "").strip() or did
        document_titles[did] = title
        if current_job_result_id:
            job_result_id = str(current_job_result_id)
            current_job_result_ids.add(job_result_id)
            document_revisions.append((did, job_result_id))

    job_result_rows = await db.execute(
        select(JobResult.id, JobResult.job_id).where(
            JobResult.id.in_(list(current_job_result_ids))
        )
    )
    job_id_by_result_id = {
        str(job_result_id): str(job_id)
        for job_result_id, job_id in job_result_rows.all()
        if job_result_id and job_id
    }

    sections_by_doc, section_path_by_id = await _load_sections(
        db,
        document_revisions=document_revisions,
        exclude_sections=excluded_secs,
    )
    units_by_doc, chunk_ref_index = await _load_chunks(
        db,
        document_revisions=document_revisions,
        exclude_sections=excluded_secs,
        section_path_by_id=section_path_by_id,
        job_id_by_result_id=job_id_by_result_id,
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
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
) -> tuple[dict[str, list[SectionRow]], dict[str, str]]:
    # Captured pairs replace DocumentSection.job_result_id == Document.current_job_result_id.
    stmt = (
        select(
            DocumentSection.document_id,
            DocumentSection.section_id,
            DocumentSection.parent_section_id,
            DocumentSection.section_path,
            DocumentSection.section_title,
            DocumentSection.section_level,
            DocumentSection.summary,
            DocumentSection.sort_order,
        )
        .where(
            tuple_(
                DocumentSection.document_id,
                DocumentSection.job_result_id,
            ).in_(document_revisions)
        )
        .order_by(
            DocumentSection.document_id,
            DocumentSection.sort_order,
            DocumentSection.section_id,
        )
    )

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
    document_revisions: list[tuple[str, str]],
    exclude_sections: list[dict[str, str]],
    section_path_by_id: dict[str, str],
    job_id_by_result_id: dict[str, str],
) -> tuple[dict[str, list[UnitRow]], dict[str, dict[str, Any]]]:
    # Captured pairs replace DocumentChunk.document_id == document_id and DocumentChunk.job_result_id == job_result_id.
    by_doc: dict[str, list[UnitRow]] = {}
    ref_index: dict[str, dict[str, Any]] = {}
    last_key: tuple[str, str, int, str, str] | None = None
    while True:
        stmt = (
            select(
                DocumentChunk.document_id,
                DocumentChunk.job_result_id,
                DocumentChunk.chunk_id,
                DocumentChunk.section_id,
                DocumentChunk.chunk_type,
                DocumentChunk.content,
                DocumentChunk.sort_order,
                DocumentChunk.source_chunk_path,
                DocumentChunk.file_path,
                DocumentChunk.chunk_metadata,
                DocumentChunk.id,
            )
            .where(
                tuple_(DocumentChunk.document_id, DocumentChunk.job_result_id).in_(
                    document_revisions
                )
            )
            .order_by(
                DocumentChunk.document_id,
                DocumentChunk.job_result_id,
                DocumentChunk.sort_order,
                DocumentChunk.chunk_id,
                DocumentChunk.id,
            )
            .limit(_CHUNK_BATCH_SIZE)
        )
        if last_key is not None:
            stmt = stmt.where(
                tuple_(
                    DocumentChunk.document_id,
                    DocumentChunk.job_result_id,
                    DocumentChunk.sort_order,
                    DocumentChunk.chunk_id,
                    DocumentChunk.id,
                )
                > tuple_(
                    literal(last_key[0]),
                    literal(last_key[1]),
                    literal(last_key[2]),
                    literal(last_key[3]),
                    literal(last_key[4]),
                )
            )

        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        for row in rows:
            document_id = str(row[0])
            job_result_id = str(row[1])
            chunk_id = str(row[2] or "").strip()
            section_id = str(row[3]) if row[3] else None
            section_path: str | None = (
                section_path_by_id.get(section_id) if section_id else None
            )
            if is_excluded_section(
                document_id=document_id,
                section_path=section_path,
                exclude_sections=exclude_sections,
            ):
                continue
            if section_id and section_id not in section_path_by_id:
                continue

            unit = UnitRow(
                chunk_id=chunk_id,
                section_id=section_id,
                chunk_type=str(row[4] or "text"),
                content=str(row[5] or ""),
                sort_order=int(row[6] or 0),
                source_chunk_path=str(row[7] or ""),
                file_path=str(row[8] or ""),
                metadata=_as_meta(row[9]),
            )
            by_doc.setdefault(document_id, []).append(unit)
            if chunk_id:
                meta = {
                    "document_id": document_id,
                    "section_path": section_path,
                    "chunk_type": unit.chunk_type,
                    "file_path": unit.file_path or None,
                    "job_id": job_id_by_result_id.get(job_result_id),
                }
                # Bare chunk_id (last-wins) plus doc-scoped key so the same
                # chunk_id can appear under multiple documents.
                ref_index[chunk_id] = meta
                ref_index[f"{document_id}:{chunk_id}"] = meta
        last_row = rows[-1]
        last_key = (
            str(last_row[0]),
            str(last_row[1]),
            int(last_row[6] or 0),
            str(last_row[2] or ""),
            str(last_row[10]),
        )
        if len(rows) < _CHUNK_BATCH_SIZE:
            break
    return by_doc, ref_index


def _as_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}
