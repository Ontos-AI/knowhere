"""Relation-aware SAME-AS page evidence resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from loguru import logger

from shared.services.chunks.same_as_markers import SAME_AS_RELATION
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type

_SAME_AS_MAX_HOPS = 3


@dataclass(frozen=True)
class PageEvidenceResolution:
    """Internal evidence resolved for a matched page row."""

    matched_chunk_id: str
    content_chunk_ids: list[str] = field(default_factory=list)
    summary: str = ""
    entities: list[dict[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    content_source: str = "summary"


def iter_connections(
    row: Mapping[str, Any],
    *,
    relations: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Yield ``connect_to`` payloads, optionally filtered by relation."""
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []

    allowed = (
        {str(relation).strip() for relation in relations if str(relation).strip()}
        if relations is not None
        else None
    )
    connections: list[dict[str, Any]] = []
    for item in metadata.get("connect_to") or []:
        if not isinstance(item, dict):
            continue
        relation = str(item.get("relation") or "related").strip()
        if allowed is not None and relation not in allowed:
            continue
        target = str(item.get("target") or "").strip()
        if not target:
            continue
        connections.append(dict(item))
    return connections


def iter_connected_target_ids(
    row: Mapping[str, Any],
    *,
    relations: Iterable[str] | None = None,
) -> list[str]:
    """Return connected target ids, optionally filtered by relation."""
    return [
        str(item.get("target") or "").strip()
        for item in iter_connections(row, relations=relations)
        if str(item.get("target") or "").strip()
    ]


def resolve_page_evidence(
    row: Mapping[str, Any],
    *,
    rows_by_chunk_id: Mapping[str, Mapping[str, Any]],
    max_hops: int = _SAME_AS_MAX_HOPS,
) -> PageEvidenceResolution:
    """Resolve page evidence for a matched row using ``same_as`` connections only.

    Public citation identity stays on the matched alias/owner row. Returned
    ``content_chunk_ids`` are internal evidence sources and must not be projected
    into public retrieval fields.
    """
    matched_chunk_id = str(row.get("chunk_id") or "").strip()
    own_summary, own_entities, own_keywords = _row_semantics(row)
    owned_pages = _owned_page_nums(row)
    page_nums = _page_nums(row)
    same_as_connections = _sorted_same_as_connections(row)

    if not same_as_connections:
        return PageEvidenceResolution(
            matched_chunk_id=matched_chunk_id,
            content_chunk_ids=[matched_chunk_id] if matched_chunk_id else [],
            summary=own_summary,
            entities=own_entities,
            keywords=own_keywords,
            content_source="summary",
        )

    owner_by_page: dict[int, Mapping[str, Any]] = {}
    content_chunk_ids: list[str] = []
    if owned_pages and matched_chunk_id:
        content_chunk_ids.append(matched_chunk_id)

    for connection in same_as_connections:
        page = _connection_page(connection)
        owner = _resolve_same_as_owner(
            row,
            connection=connection,
            rows_by_chunk_id=rows_by_chunk_id,
            max_hops=max_hops,
        )
        if owner is None:
            continue
        owner_id = str(owner.get("chunk_id") or "").strip()
        if owner_id and owner_id not in content_chunk_ids:
            content_chunk_ids.append(owner_id)
        if page is not None:
            owner_by_page[page] = owner

    if not owner_by_page and not content_chunk_ids[1:]:
        logger.warning(
            "[same_as] failed to resolve owners for matched chunk {}; "
            "keeping alias semantics",
            matched_chunk_id or "<unknown>",
        )
        return PageEvidenceResolution(
            matched_chunk_id=matched_chunk_id,
            content_chunk_ids=[matched_chunk_id] if matched_chunk_id else [],
            summary=own_summary,
            entities=own_entities,
            keywords=own_keywords,
            content_source="summary",
        )

    if not owned_pages:
        summary, entities, keywords = _merge_owner_semantics(
            owner_by_page,
            page_order=page_nums or sorted(owner_by_page),
        )
        return PageEvidenceResolution(
            matched_chunk_id=matched_chunk_id,
            content_chunk_ids=content_chunk_ids,
            summary=summary,
            entities=entities,
            keywords=keywords,
            content_source="same_as_owner_summary",
        )

    summary, entities, keywords = _merge_mixed_semantics(
        own_summary=own_summary,
        own_entities=own_entities,
        own_keywords=own_keywords,
        owned_pages=owned_pages,
        owner_by_page=owner_by_page,
        page_order=page_nums or sorted({*owned_pages, *owner_by_page}),
    )
    return PageEvidenceResolution(
        matched_chunk_id=matched_chunk_id,
        content_chunk_ids=content_chunk_ids,
        summary=summary,
        entities=entities,
        keywords=keywords,
        content_source="mixed_page_summary",
    )


def materialize_page_evidence(
    row: dict[str, Any],
    *,
    rows_by_chunk_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a shallow-copied page row with SAME-AS semantics materialized."""
    if normalize_chunk_type(row.get("chunk_type") or row.get("type")) != "page":
        return row

    resolution = resolve_page_evidence(row, rows_by_chunk_id=rows_by_chunk_id)
    updated = dict(row)
    metadata = dict(updated.get("chunk_metadata") or updated.get("metadata") or {})
    metadata["summary"] = resolution.summary
    metadata["entities"] = list(resolution.entities)
    metadata["keywords"] = list(resolution.keywords)
    updated["chunk_metadata"] = metadata
    if "metadata" in updated and isinstance(updated["metadata"], dict):
        updated["metadata"] = {
            **updated["metadata"],
            "summary": resolution.summary,
            "entities": list(resolution.entities),
            "keywords": list(resolution.keywords),
        }
    updated["content_source"] = resolution.content_source
    # Internal-only fields for assembly/debug; excluded from PUBLIC_RESULT_FIELDS.
    updated["_content_chunk_ids"] = list(resolution.content_chunk_ids)
    return updated


def resolve_navigation_summary(
    chunk: Mapping[str, Any],
    *,
    chunks_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    """Best navigation summary for a page chunk without leaking SAME-AS markers."""
    metadata = chunk.get("metadata") or chunk.get("chunk_metadata") or {}
    if isinstance(metadata, dict):
        summary = str(metadata.get("summary") or "").strip()
        if summary:
            return " ".join(summary.split())

    if normalize_chunk_type(chunk.get("type") or chunk.get("chunk_type")) != "page":
        return ""

    # ZIP/nav builders may only have the package chunk list; reuse resolver.
    package_rows = {
        str(item.get("chunk_id") or "").strip(): _package_chunk_as_row(item)
        for item in chunks_by_id.values()
        if str(item.get("chunk_id") or "").strip()
    }
    row = _package_chunk_as_row(chunk)
    resolution = resolve_page_evidence(row, rows_by_chunk_id=package_rows)
    return " ".join(resolution.summary.split()) if resolution.summary else ""


def _package_chunk_as_row(chunk: Mapping[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata") or chunk.get("chunk_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "chunk_id": chunk.get("chunk_id"),
        "chunk_type": chunk.get("type") or chunk.get("chunk_type") or "page",
        "document_id": chunk.get("document_id"),
        "job_result_id": chunk.get("job_result_id"),
        "chunk_metadata": metadata,
        "content": chunk.get("content") or "",
    }


def _sorted_same_as_connections(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    connections = iter_connections(row, relations={SAME_AS_RELATION})
    return sorted(
        connections,
        key=lambda item: (
            _connection_page(item) is None,
            _connection_page(item) or 0,
            str(item.get("target") or ""),
        ),
    )


def _resolve_same_as_owner(
    matched_row: Mapping[str, Any],
    *,
    connection: Mapping[str, Any],
    rows_by_chunk_id: Mapping[str, Mapping[str, Any]],
    max_hops: int,
) -> Mapping[str, Any] | None:
    target_id = str(connection.get("target") or "").strip()
    page = _connection_page(connection)
    matched_id = str(matched_row.get("chunk_id") or "").strip()
    if not target_id:
        return None
    if target_id == matched_id:
        logger.warning(
            "[same_as] self-loop ignored for chunk {} page={}",
            matched_id,
            page,
        )
        return None

    visited: set[str] = {matched_id} if matched_id else set()
    current_id = target_id
    hops = 0
    while current_id and hops < max_hops:
        if current_id in visited:
            logger.warning(
                "[same_as] cycle detected while resolving {} from {}",
                current_id,
                matched_id,
            )
            return None
        visited.add(current_id)
        owner = rows_by_chunk_id.get(current_id)
        if owner is None:
            logger.warning(
                "[same_as] missing owner target {} for chunk {}",
                current_id,
                matched_id,
            )
            return None
        if not _same_revision(matched_row, owner):
            logger.warning(
                "[same_as] cross-revision owner {} ignored for chunk {}",
                current_id,
                matched_id,
            )
            return None
        if normalize_chunk_type(owner.get("chunk_type") or owner.get("type")) != "page":
            logger.warning(
                "[same_as] non-page owner {} ignored for chunk {}",
                current_id,
                matched_id,
            )
            return None

        owned_pages = _owned_page_nums(owner)
        if page is not None and owned_pages and page not in owned_pages:
            # Owner may itself be an alias; follow one more same_as hop for that page.
            next_target = _same_as_target_for_page(owner, page)
            if next_target and next_target not in visited:
                current_id = next_target
                hops += 1
                continue
            logger.warning(
                "[same_as] owner {} does not own page {} for chunk {}",
                current_id,
                page,
                matched_id,
            )
            return None
        return owner

    logger.warning(
        "[same_as] max hops exceeded resolving {} for chunk {}",
        target_id,
        matched_id,
    )
    return None


def _same_as_target_for_page(row: Mapping[str, Any], page: int) -> str | None:
    for connection in iter_connections(row, relations={SAME_AS_RELATION}):
        if _connection_page(connection) == page:
            target = str(connection.get("target") or "").strip()
            return target or None
    return None


def _same_revision(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_doc = str(left.get("document_id") or "").strip()
    right_doc = str(right.get("document_id") or "").strip()
    if left_doc and right_doc and left_doc != right_doc:
        return False
    left_rev = str(left.get("job_result_id") or "").strip()
    right_rev = str(right.get("job_result_id") or "").strip()
    if left_rev and right_rev and left_rev != right_rev:
        return False
    return True


def _merge_owner_semantics(
    owner_by_page: Mapping[int, Mapping[str, Any]],
    *,
    page_order: list[int],
) -> tuple[str, list[dict[str, str]], list[str]]:
    if len(owner_by_page) == 1:
        owner = next(iter(owner_by_page.values()))
        return _row_semantics(owner)

    page_lines: list[str] = []
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for page in page_order:
        owner = owner_by_page.get(page)
        if owner is None:
            continue
        summary, owner_entities, _keywords = _row_semantics(owner)
        if summary:
            page_lines.append(f"Page {page}: {summary}")
        for entity in owner_entities:
            key = (entity["type"].casefold(), entity["text"].casefold())
            if key in seen:
                continue
            seen.add(key)
            entities.append(entity)
    keywords = [entity["text"] for entity in entities]
    return "\n".join(page_lines).strip(), entities, keywords


def _merge_mixed_semantics(
    *,
    own_summary: str,
    own_entities: list[dict[str, str]],
    own_keywords: list[str],
    owned_pages: list[int],
    owner_by_page: Mapping[int, Mapping[str, Any]],
    page_order: list[int],
) -> tuple[str, list[dict[str, str]], list[str]]:
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity in own_entities:
        key = (entity["type"].casefold(), entity["text"].casefold())
        if key in seen:
            continue
        seen.add(key)
        entities.append(entity)

    parts: list[str] = []
    if own_summary:
        parts.append(own_summary)

    for page in page_order:
        if page in owned_pages:
            continue
        owner = owner_by_page.get(page)
        if owner is None:
            continue
        summary, owner_entities, _keywords = _row_semantics(owner)
        if summary:
            parts.append(f"Page {page}: {summary}")
        for entity in owner_entities:
            key = (entity["type"].casefold(), entity["text"].casefold())
            if key in seen:
                continue
            seen.add(key)
            entities.append(entity)

    keywords = [entity["text"] for entity in entities] or list(own_keywords)
    return "\n\n".join(parts).strip(), entities, keywords


def _row_semantics(
    row: Mapping[str, Any],
) -> tuple[str, list[dict[str, str]], list[str]]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    summary = str(metadata.get("summary") or "").strip()
    raw_entities = metadata.get("entities") or []
    entities: list[dict[str, str]] = []
    if isinstance(raw_entities, list):
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            entity_type = str(item.get("type") or "").strip()
            if text and entity_type:
                entities.append({"text": text, "type": entity_type})
    keywords = metadata.get("keywords") or []
    if isinstance(keywords, list):
        keyword_list = [str(item).strip() for item in keywords if str(item).strip()]
    else:
        keyword_list = [part.strip() for part in str(keywords).split(";") if part.strip()]
    if not keyword_list:
        keyword_list = [entity["text"] for entity in entities]
    return summary, entities, keyword_list


def _owned_page_nums(row: Mapping[str, Any]) -> list[int]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    return _coerce_page_list(metadata.get("owned_page_nums"))


def _page_nums(row: Mapping[str, Any]) -> list[int]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    return _coerce_page_list(metadata.get("page_nums"))


def _coerce_page_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    pages: list[int] = []
    for item in value:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.append(page)
    return pages


def _connection_page(connection: Mapping[str, Any]) -> int | None:
    page = connection.get("page")
    if page is None:
        return None
    try:
        value = int(page)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


__all__ = [
    "PageEvidenceResolution",
    "iter_connected_target_ids",
    "iter_connections",
    "materialize_page_evidence",
    "resolve_navigation_summary",
    "resolve_page_evidence",
]
