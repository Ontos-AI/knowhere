"""Resolve ``finish.refs`` into the chunk_id-bearing shape ``resolve_workflow_references`` requires.

Verified live: ``resolve_workflow_references`` -> ``hydrate_referenced_chunk_rows``
drops any ref whose lookup key has an empty ``chunk_id`` (``row_utils.
build_reference_lookup_key`` + the ``ref_keys = [k for k in ref_keys if k[0]
and k[1]]`` guard in ``hydration/reference.py``) — a ``{document_id,
section_path}``-only ref silently resolves to zero ``referenced_chunks``,
which is exactly the shape ``agent_tools.CORPUS_SCHEMA.md``/``corpus.read``
teaches the agent to cite (``corpus.read``'s rendered ``text`` — the only
thing the LLM ever sees — shows ``section_path``, never ``chunk_id``;
``chunk_id`` only appears in its structured ``payload``/``refs``, which the
LLM does not see). This module closes that gap at the harness boundary
instead of changing what the agent is taught to cite: for any ref missing
``chunk_id``, resolve that section's own body chunk — the same "one section,
one body chunk" lookup ``corpus.read``'s ``section_path`` branch performs
(``agent_tools/section_path_lookup.py``), including the same suffix fallback
when the agent cites a path without ancestor prefixes.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.section_path_lookup import (
    resolve_section_path_anchor,
)

_BODY_CHUNK_TYPES = ("text", "page")


async def resolve_finish_refs(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    refs: list[dict[str, Any]],
    document_scope: DocumentScope = DocumentScope(),
) -> list[dict[str, Any]]:
    """Return refs with ``chunk_id`` populated; drops refs that don't resolve."""
    refs = [ref for ref in refs if document_scope.allows(str(ref.get("document_id") or "").strip())]
    document_ids = {
        str(ref.get("document_id") or "").strip() for ref in refs if ref.get("document_id")
    }
    if not document_ids:
        return []

    documents = (
        (
            await db.execute(
                select(Document)
                .where(Document.document_id.in_(document_ids))
                .where(Document.user_id == user_id)
                .where(Document.namespace == namespace)
                .where(Document.status == "active")
                .where(document_scope.predicate(Document.document_id))
            )
        )
        .scalars()
        .all()
    )
    revision_by_doc = {
        d.document_id: d.current_job_result_id for d in documents if d.current_job_result_id
    }

    resolved: list[dict[str, Any]] = []
    for ref in refs:
        document_id = str(ref.get("document_id") or "").strip()
        chunk_id = str(ref.get("chunk_id") or "").strip()
        if document_id and chunk_id:
            resolved.append({"document_id": document_id, "chunk_id": chunk_id})
            continue

        section_path = str(ref.get("section_path") or "").strip()
        job_result_id = revision_by_doc.get(document_id)
        if not (document_id and section_path and job_result_id):
            continue

        resolved_path, path_error = await resolve_section_path_anchor(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            section_path=section_path,
        )
        if path_error or not resolved_path:
            continue

        row = (
            await db.execute(
                select(DocumentChunk.chunk_id)
                .select_from(DocumentChunk)
                .join(
                    DocumentSection,
                    DocumentSection.section_id == DocumentChunk.section_id,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentSection.section_path == resolved_path)
                .where(DocumentChunk.chunk_type.in_(_BODY_CHUNK_TYPES))
            )
        ).first()
        if row is None:
            continue
        resolved.append({"document_id": document_id, "chunk_id": str(row[0])})

    return resolved
