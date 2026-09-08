"""Shared section_path resolution for agent tools and harness bridge.

``corpus.read`` and ``resolve_finish_refs`` both need to turn an agent-supplied
``section_path`` into one canonical DB path. Agents often cite a suffix (e.g.
``3 工程地质 / 3.2 覆盖层``) while the stored path includes ancestors
(``附件目录 / 3 工程地质 / 3.2 覆盖层``). Exact match alone fails silently
downstream; this module adds a segment-bound suffix fallback and surfaces
ambiguity instead of guessing.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentSection
from shared.services.retrieval.search.lexical_text import normalize_section_path


def paths_matching_section_ref(normalized: str, candidate_paths: list[str]) -> list[str]:
    """Return paths that equal ``normalized`` or end with `` / {normalized}``."""
    if not normalized:
        return []
    suffix = f" / {normalized}"
    matches = [
        path
        for path in candidate_paths
        if path == normalized or (normalized != "Root" and path.endswith(suffix))
    ]
    return sorted(dict.fromkeys(matches))


def format_ambiguous_section_path_error(normalized: str, matches: list[str]) -> str:
    return (
        f"ambiguous section_path {normalized!r}: matches {len(matches)} sections — "
        "use the full path from outline/grep/refs: "
        + "; ".join(matches)
    )


def section_path_anchor_filter(resolved_path: str):
    """SQL filter for one section (``mode=self`` anchor)."""
    return DocumentSection.section_path == resolved_path


def section_path_subtree_filter(resolved_path: str):
    """SQL filter for a section and all descendants (``mode=descendants``)."""
    return or_(
        DocumentSection.section_path == resolved_path,
        DocumentSection.section_path.like(f"{resolved_path} / %"),
    )


async def resolve_section_path_anchor(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    section_path: str,
) -> tuple[str | None, str | None]:
    """Resolve one canonical ``section_path`` or return ``(None, error)``."""
    normalized = normalize_section_path(section_path)
    base = (
        select(DocumentSection.section_path)
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
    )

    exact_row = (await db.execute(base.where(DocumentSection.section_path == normalized))).first()
    if exact_row is not None and exact_row[0]:
        return str(exact_row[0]), None

    suffix_filter = or_(
        DocumentSection.section_path == normalized,
        DocumentSection.section_path.like(f"% / {normalized}"),
    )
    suffix_rows = (
        await db.execute(base.where(suffix_filter).order_by(DocumentSection.sort_order))
    ).all()
    matches = paths_matching_section_ref(
        normalized, [str(row[0]) for row in suffix_rows if row and row[0]]
    )
    if not matches:
        return None, f"unknown section_path for {document_id}: {normalized}"
    if len(matches) > 1:
        return None, format_ambiguous_section_path_error(normalized, matches)
    return matches[0], None
