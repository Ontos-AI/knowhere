"""Shared ``scope`` parameter: narrow a tool call to one or more documents,
optionally to a section subtree within each.

Unifies what used to be five different narrowing parameters (``outline``'s
``document_id``/``path_prefix``, and ``document_ids`` on ``node_filter``,
``grep``, ``recall``, ``assets``) into one shape every map/hit tool shares:
``scope: [{document_id, section_path?}]``. Omitting ``section_path`` scopes to
the whole document; a given ``section_path`` scopes to that section and
everything under it.

A ``section_path`` that does not exist on the document's current revision is
an error — this never silently narrows to "whatever did resolve" or falls
back to the whole document. This is intentionally stricter than
``corpus.read``'s suffix-tolerant ``section_path_lookup.resolve_section_path_anchor``:
that module is a citation convenience for a model that may abbreviate a path
when *reading*; ``scope`` is a caller-declared boundary for *searching*, so
an exact match is required — the caller got this path from a prior
``outline``/``node_filter``/hit row and should copy it verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentSection
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.search.lexical_text import normalize_section_path

SCOPE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "description": "Document to include in this call's scope.",
        },
        "section_path": {
            "type": "string",
            "description": (
                "Restrict this document to this section and everything "
                "under it. Must be the exact section_path from a prior "
                "outline/node_filter/grep/recall row — not a partial or "
                "ancestor-omitting path. Omit for the whole document."
            ),
        },
    },
    "required": ["document_id"],
    "additionalProperties": False,
}

SCOPE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": SCOPE_ITEM_SCHEMA,
    "minItems": 1,
    "description": (
        "One or more {document_id, section_path?} targets to search. "
        "Omitting section_path on an item scopes to that whole document; "
        "giving one scopes to that section and its full subtree. An "
        "unknown document_id or section_path fails the call instead of "
        "silently searching less than requested."
    ),
}


@dataclass(frozen=True)
class ScopeTarget:
    document_id: str
    job_result_id: str
    # ``None`` means the whole document; otherwise the exact, already
    # section_path-normalized subtree root.
    section_path: str | None


async def resolve_scope(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    document_scope: DocumentScope,
    raw_scope: Any,
) -> tuple[list[ScopeTarget], str | None]:
    """Resolve a ``scope`` argument into concrete targets, or an error string.

    Every item must name a document the caller can see and, when given, an
    exact ``section_path`` on that document's current revision. Any single
    failure fails the whole call.
    """
    items = raw_scope if isinstance(raw_scope, list) else []
    if not items:
        return [], "scope requires at least one {document_id, section_path?} item"

    document_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return [], f"scope item must be an object with document_id, got {item!r}"
        document_id = str(item.get("document_id") or "").strip()
        if not document_id:
            return [], "scope item requires document_id"
        document_ids.append(document_id)

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

    targets: list[ScopeTarget] = []
    for item in items:
        document_id = str(item.get("document_id") or "").strip()
        job_result_id = revision_by_doc.get(document_id)
        if not job_result_id:
            return [], f"unknown document_id: {document_id}"
        raw_path = str(item.get("section_path") or "").strip()
        if not raw_path:
            targets.append(ScopeTarget(document_id, job_result_id, None))
            continue
        normalized = normalize_section_path(raw_path)
        exists = (
            await db.execute(
                select(DocumentSection.section_id)
                .where(DocumentSection.document_id == document_id)
                .where(DocumentSection.job_result_id == job_result_id)
                .where(DocumentSection.section_path == normalized)
            )
        ).first()
        if exists is None:
            return [], (
                f"unknown section_path for {document_id}: {normalized!r} — "
                "use the exact section_path from a prior outline/node_filter/"
                "grep/recall row"
            )
        targets.append(ScopeTarget(document_id, job_result_id, normalized))
    return targets, None


def scope_document_ids(scope: list[ScopeTarget]) -> list[str]:
    return sorted({target.document_id for target in scope})


def scope_orm_clause(
    scope: list[ScopeTarget],
    *,
    document_id_col: Any,
    section_path_col: Any,
) -> Any:
    """OR of per-target ``(document_id AND subtree)`` clauses for an ORM query."""
    per_target = []
    for target in scope:
        clauses: list[Any] = [document_id_col == target.document_id]
        if target.section_path is not None:
            clauses.append(
                or_(
                    section_path_col == target.section_path,
                    section_path_col.like(f"{target.section_path} / %"),
                )
            )
        per_target.append(and_(*clauses))
    return or_(*per_target)
