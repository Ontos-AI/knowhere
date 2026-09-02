"""Classic-route discovery via the persisted map-unit BM25 scorer.

Replaces the retired chunk-level 3-channel SQL scan. Scoring uses the same
``score_persisted_corpus_many`` formula as map-nav (path + content only).

``chunk_types`` is optional: omitted means score every in-scope unit. When
the request is image/table only, ``has_image`` / ``has_table`` (written at
index time) narrow candidates before scoring.

Hydration returns the one chunk owned by each winning leaf. Images and
tables are not sibling chunks on that leaf; they enter through
``connect_to``. Image/table-only requests follow ``connect_to`` from
that chunk. Failures raise; they are not converted into empty results.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.row_utils import (
    iter_connected_target_ids,
    normalize_chunk_type,
)
from shared.services.retrieval.nav.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_persisted_corpus_many,
    tokenize_query_for_ranker,
)
from shared.services.retrieval.nav.persisted_score_load import (
    build_channel_bm25_stats,
    combine_average_idf,
)
from shared.services.retrieval.cache_service import record_retrieval_index_readiness
from shared.services.retrieval.search.scoring import normalize_row_scores
from shared.services.retrieval.search.section_filters import is_excluded_section
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

_MAP_SCORE_CHANNELS = ("path", "content")

_SCOPED_UNITS_CTE = """
WITH scoped_units AS (
    SELECT
        dmu.id AS map_unit_id,
        dmu.document_id,
        dmu.job_result_id,
        dmu.section_id,
        dmu.path_token_count,
        dmu.content_token_count,
        ds.section_path
    FROM document_map_units dmu
    JOIN documents d
        ON d.document_id = dmu.document_id
        {revision_join}
    JOIN document_sections ds
        ON ds.section_id = dmu.section_id
    WHERE d.user_id = :user_id
        AND d.namespace = :namespace
        AND d.status = 'active'
        {revision_clause}
        {exclude_clause}
        {type_clause}
        {signal_clause}
)
"""

_SCOPED_UNIT_IDS_CTE = """
WITH scoped_units AS (
    SELECT
        dmu.id AS map_unit_id,
        dmu.document_id,
        dmu.job_result_id
    FROM document_map_units dmu
    JOIN documents d
        ON d.document_id = dmu.document_id
        {revision_join}
    WHERE d.user_id = :user_id
        AND d.namespace = :namespace
        AND d.status = 'active'
        {revision_clause}
        {exclude_clause}
        {type_clause}
)
"""


@dataclass
class DiscoveryResult:
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None


def _build_revision_scope(
    revision_pins: Mapping[str, str] | None,
) -> tuple[str, str, dict[str, Any]]:
    if revision_pins is None:
        return ("AND d.current_job_result_id = dmu.job_result_id", "", {})
    pairs = [
        (str(document_id).strip(), str(job_result_id).strip())
        for document_id, job_result_id in revision_pins.items()
        if str(document_id).strip() and str(job_result_id).strip()
    ]
    if not pairs:
        return "", "AND FALSE", {}
    params: dict[str, Any] = {}
    placeholders: list[str] = []
    for index, (document_id, job_result_id) in enumerate(pairs):
        document_key = f"_pin_document_{index}"
        revision_key = f"_pin_revision_{index}"
        placeholders.append(f"(:{document_key}, :{revision_key})")
        params[document_key] = document_id
        params[revision_key] = job_result_id
    return (
        "",
        f"AND (dmu.document_id, dmu.job_result_id) IN ({', '.join(placeholders)})",
        params,
    )


def _build_type_clause(
    allowed_chunk_types: set[str] | None,
) -> tuple[str, dict[str, Any]]:
    if allowed_chunk_types is None or not allowed_chunk_types.issubset(
        ASSET_CHUNK_TYPES
    ):
        return "", {}
    clauses = []
    if "image" in allowed_chunk_types:
        clauses.append("dmu.has_image")
    if "table" in allowed_chunk_types:
        clauses.append("dmu.has_table")
    if not clauses:
        return "AND FALSE", {}
    return f"AND ({' OR '.join(clauses)})", {}


def _build_exclude_clause(exclude_document_ids: list[str]) -> tuple[str, dict[str, Any]]:
    if not exclude_document_ids:
        return "", {}
    return "AND d.document_id <> ALL(:excluded_doc_ids)", {
        "excluded_doc_ids": list(exclude_document_ids)
    }


def _build_signal_clause(
    signal_paths: list[str], filter_mode: str
) -> tuple[str, dict[str, Any]]:
    if not signal_paths:
        return "", {}
    ilike_parts = []
    params: dict[str, Any] = {}
    for index, keyword in enumerate(signal_paths):
        key = f"_sig_{index}"
        ilike_parts.append(f"LOWER(COALESCE(ds.section_path, '')) LIKE :{key}")
        params[key] = f"%{keyword.lower()}%"
    combined = " OR ".join(ilike_parts)
    clause = f"AND ({combined})" if filter_mode == "keep" else f"AND NOT ({combined})"
    return clause, params


async def map_unit_discovery(
    db: AsyncSession | None,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    revision_pins: Mapping[str, str] | None = None,
    **_kwargs: Any,
) -> DiscoveryResult:
    """Score the whole in-scope corpus via the persisted map-unit BM25 scorer."""
    t0 = time.monotonic()
    if db is None:
        raise ValueError("database session required for map_unit_discovery")
    query_tokens = tokenize_query_for_ranker(query)
    if not query_tokens:
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})
    query_token_hashes = [
        sha256(token.encode("utf-8")).hexdigest() for token in query_tokens
    ]

    revision_join, revision_clause, revision_params = _build_revision_scope(
        revision_pins
    )
    exclude_clause, exclude_params = _build_exclude_clause(exclude_document_ids)
    type_clause, type_params = _build_type_clause(chunk_types)
    signal_clause, signal_params = _build_signal_clause(
        signal_paths or [], filter_mode
    )
    params: dict[str, Any] = {"user_id": user_id, "namespace": namespace}
    params.update(revision_params)
    params.update(exclude_params)
    params.update(type_params)
    params.update(signal_params)

    is_unfiltered_scope: bool = not any(
        (
            chunk_types,
            signal_paths,
            exclude_sections,
            exclude_document_ids,
        )
    )

    cte = _SCOPED_UNITS_CTE.format(
        revision_join=revision_join,
        revision_clause=revision_clause,
        exclude_clause=exclude_clause,
        type_clause=type_clause,
        signal_clause=signal_clause,
    )
    unit_statement = cte + "SELECT * FROM scoped_units"
    if is_unfiltered_scope:
        unit_statement = (
            "WITH matching_tokens AS MATERIALIZED ("
            "SELECT DISTINCT map_unit_id FROM document_map_unit_tokens "
            "WHERE channel = ANY(:channels) AND token_hash = ANY(:token_hashes)"
            "), "
            + cte.lstrip().removeprefix("WITH ")
            + " SELECT DISTINCT scoped_units.* "
            "FROM matching_tokens JOIN scoped_units "
            "ON scoped_units.map_unit_id = matching_tokens.map_unit_id"
        )
        params = {
            **params,
            "channels": list(_MAP_SCORE_CHANNELS),
            "token_hashes": query_token_hashes,
        }
    unit_result = await db.execute(text(unit_statement), params)
    unit_rows = [dict(row._mapping) for row in unit_result.all()]
    unit_rows = [
        row
        for row in unit_rows
        if not is_excluded_section(
            document_id=row.get("document_id"),
            section_path=row.get("section_path"),
            exclude_sections=exclude_sections,
        )
    ]

    if not unit_rows:
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    frequency_scope_cte = cte if signal_paths else _SCOPED_UNIT_IDS_CTE.format(
        revision_join=revision_join,
        revision_clause=revision_clause,
        exclude_clause=exclude_clause,
        type_clause=type_clause,
    )
    frequency_query = text(
        "WITH matching_tokens AS MATERIALIZED ("
        "SELECT map_unit_id, channel, token, frequency "
        "FROM document_map_unit_tokens "
        "WHERE channel = ANY(:channels) "
        "AND token_hash = ANY(:token_hashes)"
        "), "
        + frequency_scope_cte.lstrip().removeprefix("WITH ")
        + """
                SELECT matching_tokens.map_unit_id, matching_tokens.channel,
                       matching_tokens.token, matching_tokens.frequency
                FROM matching_tokens
                JOIN scoped_units
                    ON scoped_units.map_unit_id = matching_tokens.map_unit_id
                """
    )
    frequency_result = await db.execute(
        frequency_query,
        {
            **params,
            "channels": list(_MAP_SCORE_CHANNELS),
            "token_hashes": query_token_hashes,
        },
    )
    frequencies: dict[tuple[str, str], dict[str, int]] = {}
    for map_unit_id, channel, token, frequency in frequency_result.all():
        frequencies.setdefault((str(map_unit_id), str(channel)), {})[str(token)] = (
            int(frequency)
        )

    index_result = await db.execute(
        text(
            (
                cte
                if signal_paths
                else _SCOPED_UNIT_IDS_CTE.format(
                    revision_join=revision_join,
                    revision_clause=revision_clause,
                    exclude_clause=exclude_clause,
                    type_clause=type_clause,
                )
            )
            + """
                SELECT indexes.average_idf_path, indexes.average_idf_content,
                       indexes.unit_count, indexes.format_version,
                       indexes.path_document_count, indexes.path_total_length,
                       indexes.content_document_count, indexes.content_total_length
                FROM document_map_unit_indexes AS indexes
                JOIN (
                    SELECT DISTINCT document_id, job_result_id FROM scoped_units
                ) AS scoped_revisions
                    ON indexes.document_id = scoped_revisions.document_id
                    AND indexes.job_result_id = scoped_revisions.job_result_id
                """
        ),
        params,
    )
    index_parts = [
        (
            float(path_idf or 0.0),
            float(content_idf or 0.0),
            int(unit_count or 0),
            int(format_version or 0),
            path_document_count,
            path_total_length,
            content_document_count,
            content_total_length,
        )
        for (
            path_idf,
            content_idf,
            unit_count,
            format_version,
            path_document_count,
            path_total_length,
            content_document_count,
            content_total_length,
        ) in index_result.all()
    ]
    if is_unfiltered_scope:
        revision_result = await db.execute(
            text(cte + "SELECT DISTINCT document_id, job_result_id FROM scoped_units"),
            params,
        )
        expected_revisions = {
            (str(row[0]), str(row[1])) for row in revision_result.all()
        }
    else:
        expected_revisions = {
            (str(row["document_id"]), str(row["job_result_id"]))
            for row in unit_rows
        }
    indexed_unit_count = sum(
        unit_count for _path_idf, _content_idf, unit_count, *_rest in index_parts
    )
    has_index_unit_count_mismatch = (
        indexed_unit_count < len(unit_rows)
        if is_unfiltered_scope
        else indexed_unit_count != len(unit_rows)
    )
    is_index_format_incompatible = any(
        format_version != MAP_UNIT_INDEX_FORMAT_VERSION
        for (
            _path_idf,
            _content_idf,
            _unit_count,
            format_version,
            _path_document_count,
            _path_total_length,
            _content_document_count,
            _content_total_length,
        ) in index_parts
    )
    has_incomplete_index_statistics = is_unfiltered_scope and any(
        format_version != MAP_UNIT_INDEX_FORMAT_VERSION
        or path_document_count is None
        or path_total_length is None
        or content_document_count is None
        or content_total_length is None
        for (
            _path_idf,
            _content_idf,
            _unit_count,
            format_version,
            path_document_count,
            path_total_length,
            content_document_count,
            content_total_length,
        ) in index_parts
    )
    if (
        len(index_parts) != len(expected_revisions)
        or has_index_unit_count_mismatch
        or is_index_format_incompatible
        or has_incomplete_index_statistics
    ):
        try:
            await record_retrieval_index_readiness(
                user_id=user_id,
                namespace=namespace,
                ready=False,
                expected_revisions=len(expected_revisions),
                indexed_revisions=len(index_parts),
            )
        except Exception as exc:
            logger.warning("retrieval index readiness publish failed: %s", exc)
        logger.warning(
            "retrieval map index incomplete user_id=%s namespace=%s "
            "expected_revisions=%d indexed_revisions=%d fallback=legacy_fts",
            user_id,
            namespace,
            len(expected_revisions),
            len(index_parts),
        )
        return await _legacy_chunk_discovery(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            chunk_types=chunk_types,
            signal_paths=signal_paths or [],
            filter_mode=filter_mode,
            revision_pins=revision_pins,
        )
    try:
        await record_retrieval_index_readiness(
            user_id=user_id,
            namespace=namespace,
            ready=True,
            expected_revisions=len(expected_revisions),
            indexed_revisions=len(index_parts),
        )
    except Exception as exc:
        logger.warning("retrieval index readiness publish failed: %s", exc)
    average_idf_path = combine_average_idf(
        [
            (path_idf, unit_count)
            for path_idf, _content_idf, unit_count, *_rest in index_parts
        ]
    )
    average_idf_content = combine_average_idf(
        [
            (content_idf, unit_count)
            for _path_idf, content_idf, unit_count, *_rest in index_parts
        ]
    )

    path_stats = build_channel_bm25_stats(
        unit_rows=unit_rows,
        map_unit_id_field="map_unit_id",
        length_field="path_token_count",
        channel="path",
        query_tokens=query_tokens,
        frequencies=frequencies,
        average_idf=average_idf_path,
        document_count_override=(
            sum(int(part[4] or 0) for part in index_parts)
            if is_unfiltered_scope
            else None
        ),
        total_length_override=(
            sum(int(part[5] or 0) for part in index_parts)
            if is_unfiltered_scope
            else None
        ),
    )
    content_stats = build_channel_bm25_stats(
        unit_rows=unit_rows,
        map_unit_id_field="map_unit_id",
        length_field="content_token_count",
        channel="content",
        query_tokens=query_tokens,
        frequencies=frequencies,
        average_idf=average_idf_content,
        document_count_override=(
            sum(int(part[6] or 0) for part in index_parts)
            if is_unfiltered_scope
            else None
        ),
        total_length_override=(
            sum(int(part[7] or 0) for part in index_parts)
            if is_unfiltered_scope
            else None
        ),
    )

    corpus = PersistedScoreCorpus(
        units=[
            PersistedScoreUnit(
                unit_id=row["map_unit_id"],
                path_length=int(row["path_token_count"]),
                content_length=int(row["content_token_count"]),
                path_frequencies=frequencies.get((row["map_unit_id"], "path"), {}),
                content_frequencies=frequencies.get(
                    (row["map_unit_id"], "content"), {}
                ),
            )
            for row in unit_rows
        ],
        path_stats=path_stats,
        content_stats=content_stats,
    )
    scores_by_unit = score_persisted_corpus_many(corpus, [query]).get(query, {})

    rows_by_unit_id = {row["map_unit_id"]: row for row in unit_rows}
    ranked_unit_ids = sorted(
        (unit_id for unit_id, score in scores_by_unit.items() if score > 0.0),
        key=lambda unit_id: scores_by_unit[unit_id],
        reverse=True,
    )[:top_k]

    fused_rows = await _hydrate_winning_units(
        db,
        ranked_unit_ids=ranked_unit_ids,
        rows_by_unit_id=rows_by_unit_id,
        scores_by_unit=scores_by_unit,
        chunk_types=chunk_types,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        revision_pins=revision_pins,
    )
    if fused_rows:
        normalize_row_scores(
            fused_rows,
            source_field="score",
            target_field="discovery_score",
            default=0.5,
        )

    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        "  search.map_unit_discovery: {} units scored, {} fused rows, {}ms",
        len(unit_rows),
        len(fused_rows),
        latency,
    )
    return DiscoveryResult(
        status="discovery_done",
        payload={"fused_rows": fused_rows},
        latency_ms=latency,
    )


async def _legacy_chunk_discovery(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    chunk_types: set[str] | None,
    signal_paths: list[str],
    filter_mode: str,
    revision_pins: Mapping[str, str] | None,
) -> DiscoveryResult:
    """Bounded lexical fallback used while a serving index is incomplete."""
    clauses = [
        "d.user_id = :user_id",
        "d.namespace = :namespace",
        "d.status = 'active'",
    ]
    params: dict[str, Any] = {
        "user_id": user_id,
        "namespace": namespace,
        "query": query,
        "limit": max(1, int(top_k)),
    }
    if revision_pins is None:
        clauses.append("d.current_job_result_id = dc.job_result_id")
    else:
        pairs = [
            (str(document_id), str(job_result_id))
            for document_id, job_result_id in revision_pins.items()
        ]
        if not pairs:
            return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})
        placeholders = []
        for index, (document_id, job_result_id) in enumerate(pairs):
            document_key = f"_legacy_doc_{index}"
            revision_key = f"_legacy_revision_{index}"
            placeholders.append(f"(:{document_key}, :{revision_key})")
            params[document_key] = document_id
            params[revision_key] = job_result_id
        clauses.append(f"(dc.document_id, dc.job_result_id) IN ({', '.join(placeholders)})")
    if exclude_document_ids:
        clauses.append("d.document_id <> ALL(:excluded_doc_ids)")
        params["excluded_doc_ids"] = exclude_document_ids
    if chunk_types:
        type_keys = []
        for index, chunk_type in enumerate(sorted(chunk_types)):
            key = f"_legacy_type_{index}"
            type_keys.append(f":{key}")
            params[key] = chunk_type
        clauses.append(f"LOWER(dc.chunk_type) IN ({', '.join(type_keys)})")
    if signal_paths:
        signal_parts = []
        for index, signal in enumerate(signal_paths):
            key = f"_legacy_signal_{index}"
            signal_parts.append("LOWER(COALESCE(ds.section_path, '')) LIKE :" + key)
            params[key] = f"%{signal.lower()}%"
        combined = " OR ".join(signal_parts)
        clauses.append(f"({combined})" if filter_mode == "keep" else f"NOT ({combined})")
    for index, item in enumerate(exclude_sections):
        document_id = str(item.get("document_id") or "").strip()
        section_path = str(item.get("section_path") or "").strip()
        if not document_id or not section_path:
            continue
        doc_key = f"_legacy_exclude_doc_{index}"
        path_key = f"_legacy_exclude_path_{index}"
        params[doc_key] = document_id
        params[path_key] = section_path
        clauses.append(
            "NOT (dc.document_id = :" + doc_key + " AND ("
            "COALESCE(ds.section_path, '') = :" + path_key + " OR "
            "POSITION(:" + path_key + " || ' / ' IN COALESCE(ds.section_path, '')) = 1))"
        )
    where_sql = " AND ".join(clauses)
    statement = text(
        "SELECT dc.chunk_id, dc.document_id, dc.section_id, dc.chunk_type, "
        "dc.content, dc.source_chunk_path, dc.file_path, dc.chunk_metadata, "
        "dc.job_result_id, dc.sort_order, ds.section_path, d.source_file_name, "
        "jr.job_id, GREATEST(ts_rank_cd(dc.path_search_tsv, plainto_tsquery('simple', :query)), "
        "2 * ts_rank_cd(dc.content_search_tsv, plainto_tsquery('simple', :query))) AS score "
        "FROM document_chunks dc JOIN documents d ON d.document_id = dc.document_id "
        "LEFT JOIN document_sections ds ON ds.section_id = dc.section_id "
        "LEFT JOIN job_results jr ON jr.id = dc.job_result_id "
        f"WHERE {where_sql} AND (dc.path_search_tsv @@ plainto_tsquery('simple', :query) "
        "OR dc.content_search_tsv @@ plainto_tsquery('simple', :query) "
        "OR LOWER(COALESCE(dc.term_search_text, '')) LIKE LOWER(:term_query)) "
        "ORDER BY score DESC, dc.sort_order, dc.chunk_id LIMIT :limit"
    )
    params["term_query"] = f"%{query}%"
    rows = [dict(row._mapping) for row in (await db.execute(statement, params)).all()]
    if rows:
        normalize_row_scores(rows, source_field="score", target_field="discovery_score", default=0.5)
    return DiscoveryResult(status="discovery_done", payload={"fused_rows": rows})


def _as_metadata_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def _hydrate_winning_units(
    session: AsyncSession,
    *,
    ranked_unit_ids: list[str],
    rows_by_unit_id: dict[str, dict[str, Any]],
    scores_by_unit: dict[str, float],
    chunk_types: set[str] | None,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    revision_pins: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    """Map each winning leaf to its one chunk; asset requests follow connect_to."""
    if not ranked_unit_ids:
        return []

    document_ids = sorted(
        {rows_by_unit_id[unit_id]["document_id"] for unit_id in ranked_unit_ids}
    )
    job_result_ids = sorted(
        {rows_by_unit_id[unit_id]["job_result_id"] for unit_id in ranked_unit_ids}
    )
    section_ids = sorted(
        {rows_by_unit_id[unit_id]["section_id"] for unit_id in ranked_unit_ids}
    )

    result = await session.execute(
        text(
            "SELECT dc.chunk_id, dc.document_id, dc.section_id, dc.chunk_type, "
            "dc.content, dc.source_chunk_path, dc.file_path, dc.chunk_metadata, "
            "dc.job_result_id, dc.sort_order, ds.section_path, d.source_file_name, "
            "jr.job_id "
            "FROM document_chunks dc "
            "JOIN documents d ON d.document_id = dc.document_id "
            "LEFT JOIN document_sections ds ON ds.section_id = dc.section_id "
            "LEFT JOIN job_results jr ON jr.id = dc.job_result_id "
            "WHERE dc.document_id = ANY(:document_ids) "
            "AND dc.job_result_id = ANY(:job_result_ids) "
            "AND dc.section_id = ANY(:section_ids) "
            "ORDER BY dc.sort_order, dc.chunk_id"
        ),
        {
            "document_ids": document_ids,
            "job_result_ids": job_result_ids,
            "section_ids": section_ids,
        },
    )
    chunk_by_section: dict[tuple[str, str, str], dict[str, Any]] = {}
    for chunk_row in result.all():
        row = dict(chunk_row._mapping)
        row["chunk_metadata"] = _as_metadata_dict(row.get("chunk_metadata"))
        key = (row["document_id"], row["job_result_id"], row["section_id"])
        chunk_by_section[key] = row

    primaries: list[dict[str, Any]] = []
    for unit_id in ranked_unit_ids:
        unit_row = rows_by_unit_id[unit_id]
        key = (
            unit_row["document_id"],
            unit_row["job_result_id"],
            unit_row["section_id"],
        )
        primary = chunk_by_section.get(key)
        if primary is None:
            continue
        fused = dict(primary)
        fused["score"] = scores_by_unit.get(unit_id, 0.0)
        primaries.append(fused)

    if chunk_types is None or not chunk_types.issubset(ASSET_CHUNK_TYPES):
        return primaries

    connected = await hydrate_connected_target_rows(
        db=session,
        rows=primaries,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        revision_pins=revision_pins,
    )
    connected_by_id = {
        str(row.get("chunk_id") or ""): row
        for row in connected
        if row.get("chunk_id")
    }
    fused_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for primary in primaries:
        score = float(primary.get("score") or 0.0)
        key = (
            primary["document_id"],
            primary["job_result_id"],
            primary["section_id"],
        )
        section_chunk = chunk_by_section.get(key)
        candidates = [section_chunk] if section_chunk is not None else []
        for target_id in iter_connected_target_ids(primary):
            target = connected_by_id.get(target_id)
            if target is not None:
                candidates.append(target)
        for candidate in candidates:
            chunk_id = str(candidate.get("chunk_id") or "")
            if (
                not chunk_id
                or chunk_id in seen
                or normalize_chunk_type(candidate.get("chunk_type")) not in chunk_types
            ):
                continue
            seen.add(chunk_id)
            fused = dict(candidate)
            fused["score"] = score
            fused_rows.append(fused)
    return fused_rows
