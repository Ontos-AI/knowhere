"""
Independent retrieval channels for checkerboard search.

Each channel queries the full scoped corpus independently and returns
ranked rows. Channels are fused via RRF in the orchestrator.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.search.lexical_ranker import (
    rank_rows_by_bm25,
    tokenize_query_for_ranker,
)
from shared.services.retrieval.search.section_filters import is_excluded_section


_SCOPED_CORPUS_CTE = """
WITH scoped_chunks AS (
    SELECT
        dc.id,
        dc.chunk_id,
        dc.document_id,
        dc.section_id,
        dc.chunk_type,
        dc.content,
        dc.file_path,
        dc.chunk_metadata,
        dc.job_result_id,
        dc.sort_order,
        dc.content_search_text,
        dc.content_search_tsv,
        dc.path_search_text,
        dc.path_search_tsv,
        dc.term_search_text,
        d.source_file_name,
        d.user_id,
        d.namespace,
        ds.section_path,
        jr.job_id
    FROM document_chunks dc
    JOIN documents d
        ON d.document_id = dc.document_id
        AND d.current_job_result_id = dc.job_result_id
    LEFT JOIN document_sections ds
        ON ds.section_id = dc.section_id
    JOIN job_results jr
        ON jr.id = dc.job_result_id
    WHERE d.user_id = :user_id
        AND d.namespace = :namespace
        AND d.status = 'active'
        {exclude_clause}
        {extra_filters}
)
"""


def _build_exclude_clause(exclude_document_ids: list[str]) -> str:
    if not exclude_document_ids:
        return ""
    # Use PostgreSQL array ANY() to avoid asyncpg tuple-binding pitfalls with
    # raw text() + `NOT IN :param` (which asyncpg treats as a record parameter
    # and fails with a syntax error).
    return "AND d.document_id <> ALL(:excluded_doc_ids)"


def _build_base_params(
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "user_id": user_id,
        "namespace": namespace,
    }
    if exclude_document_ids:
        params["excluded_doc_ids"] = list(exclude_document_ids)
    return params


def _build_extra_filters(
    *,
    allowed_chunk_types: set[str] | None,
    signal_paths: list[str],
    filter_mode: str,
) -> tuple[str, dict[str, Any]]:
    """Build additional SQL WHERE clauses for data_type and signal_path filtering."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if allowed_chunk_types is not None:
        placeholders = ', '.join(f':_act_{i}' for i in range(len(allowed_chunk_types)))
        clauses.append(f'AND LOWER(dc.chunk_type) IN ({placeholders})')
        for i, ct in enumerate(sorted(allowed_chunk_types)):
            params[f'_act_{i}'] = ct

    if signal_paths:
        # TODO(intent-step): Current implementation uses OR across
        # signal_paths keywords. The Intent Step will need hierarchical
        # AND (prefix) matching, e.g. signal_paths=["第一章/1.1/（2）"]
        # should match only paths containing ALL segments in order.
        # Consider adding a `filter_strategy` param: "keyword_or" (current)
        # vs "path_prefix" (for Intent Step resolved paths).
        ilike_parts = []
        for i, kw in enumerate(signal_paths):
            key = f'_sig_{i}'
            ilike_parts.append(f"LOWER(COALESCE(ds.section_path, '')) LIKE :{key}")
            params[key] = f'%{kw.lower()}%'
        combined = ' OR '.join(ilike_parts)
        if filter_mode == 'keep':
            clauses.append(f'AND ({combined})')
        else:
            clauses.append(f'AND NOT ({combined})')

    return '\n        '.join(clauses), params


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _filter_excluded_sections(
    rows: list[dict[str, Any]],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not exclude_sections:
        return rows
    return [
        row for row in rows
        if not is_excluded_section(
            document_id=row.get("document_id"),
            section_path=row.get("section_path"),
            exclude_sections=exclude_sections,
        )
    ]


async def path_channel(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = 'delete',
) -> list[dict[str, Any]]:
    """Path channel: BM25 over pre-tokenized path search text.

    This keeps the channel useful when vector search is unavailable. A future
    vector score can be fused on top of the returned BM25 score.
    """
    return await _bm25_channel(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        allowed_chunk_types=allowed_chunk_types,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        search_field="path_search_text",
    )


async def content_channel(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = 'delete',
) -> list[dict[str, Any]]:
    """Content channel: BM25 over pre-tokenized content search text."""
    return await _bm25_channel(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        allowed_chunk_types=allowed_chunk_types,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        search_field="content_search_text",
    )


async def _bm25_channel(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None,
    signal_paths: list[str] | None,
    filter_mode: str,
    search_field: str,
) -> list[dict[str, Any]]:
    if search_field not in {"content_search_text", "path_search_text"}:
        raise ValueError(f"Unsupported search_field: {search_field}")

    query_tokens = tokenize_query_for_ranker(query)
    if not query_tokens:
        return []

    exclude_clause = _build_exclude_clause(exclude_document_ids)
    extra_sql, extra_params = _build_extra_filters(
        allowed_chunk_types=allowed_chunk_types,
        signal_paths=signal_paths or [],
        filter_mode=filter_mode,
    )
    params = _build_base_params(
        user_id=user_id,
        namespace=namespace,
        exclude_document_ids=exclude_document_ids,
    )
    params.update(extra_params)

    sql = _SCOPED_CORPUS_CTE.format(exclude_clause=exclude_clause, extra_filters=extra_sql) + f"""
    SELECT sc.*
    FROM scoped_chunks sc
    WHERE COALESCE(sc.{search_field}, '') <> ''
    """

    result = await db.execute(text(sql), params)
    rows = [_row_to_dict(r) for r in result.all()]
    rows = _filter_excluded_sections(rows, exclude_sections)

    ranked_rows = rank_rows_by_bm25(rows, query_tokens, search_field=search_field)
    return ranked_rows[:top_k]


async def term_channel(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = 'delete',
) -> list[dict[str, Any]]:
    """Term/grep channel: substring matching on term_search_text.

    Aligned with KB checkerboard_find() term channel (grep_search()):
    exact substring match on content + path, scoring by hit count.

    Note: top_k is already effective_recall_k from app_service.
    """
    query_lower = query.lower().strip()
    query_tokens = tokenize_query_for_ranker(query)
    if not query_lower or not query_tokens:
        return []

    exclude_clause = _build_exclude_clause(exclude_document_ids)
    extra_sql, extra_params = _build_extra_filters(
        allowed_chunk_types=allowed_chunk_types,
        signal_paths=signal_paths or [],
        filter_mode=filter_mode,
    )
    params = _build_base_params(
        user_id=user_id,
        namespace=namespace,
        exclude_document_ids=exclude_document_ids,
    )
    params.update(extra_params)

    ilike_conditions = []
    for i, unit in enumerate(query_tokens):
        param_key = f"unit_{i}"
        ilike_conditions.append(f"LOWER(sc.term_search_text) LIKE :{param_key}")
        params[param_key] = f"%{unit}%"

    if not ilike_conditions:
        ilike_conditions.append("LOWER(sc.term_search_text) LIKE :full_query")
        params["full_query"] = f"%{query_lower}%"

    where_clause = " OR ".join(ilike_conditions)
    sql = _SCOPED_CORPUS_CTE.format(exclude_clause=exclude_clause, extra_filters=extra_sql) + f"""
    SELECT sc.*
    FROM scoped_chunks sc
    WHERE sc.term_search_text IS NOT NULL
        AND ({where_clause})
    """

    result = await db.execute(text(sql), params)
    rows = [_row_to_dict(r) for r in result.all()]
    rows = _filter_excluded_sections(rows, exclude_sections)

    scored: list[dict[str, Any]] = []
    for row in rows:
        haystack = (row.get("term_search_text") or "").lower()
        if query_lower in haystack:
            row["score"] = 100.0
            scored.append(row)
        else:
            hit_count = sum(1 for u in query_tokens if u in haystack)
            if hit_count > 0:
                row["score"] = float(hit_count)
                scored.append(row)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_k]
