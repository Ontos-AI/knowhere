"""Classic-route discovery via the persisted map-unit BM25 scorer.

Replaces the retired chunk-level 3-channel SQL scan. Scoring uses the same
``score_persisted_corpus_many`` formula as map-nav (path + content only).

``chunk_types`` is optional: omitted means score every in-scope unit. When
the request is image/table only, ``has_image`` / ``has_table`` (written at
index time) narrow candidates before scoring.

Hydration returns one primary chunk per winning unit. Asset-only requests
then follow ``connect_to`` to that unit's image/table row. Downstream
``assemble_retrieval_results`` still inlines connected assets for text hits.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    PersistedBm25Stats,
    PersistedScoreCorpus,
    PersistedScoreUnit,
    score_persisted_corpus_many,
    tokenize_query_for_ranker,
)
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
    try:
        if db is None:
            return DiscoveryResult(
                status="error",
                error="database session required",
                latency_ms=0,
            )
        query_tokens = tokenize_query_for_ranker(query)
        if not query_tokens:
            return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

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

        cte = _SCOPED_UNITS_CTE.format(
            revision_join=revision_join,
            revision_clause=revision_clause,
            exclude_clause=exclude_clause,
            type_clause=type_clause,
            signal_clause=signal_clause,
        )
        unit_result = await db.execute(text(cte + "SELECT * FROM scoped_units"), params)
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

        map_unit_ids = [row["map_unit_id"] for row in unit_rows]
        frequency_result = await db.execute(
            text(
                "SELECT map_unit_id, channel, token, frequency "
                "FROM document_map_unit_tokens "
                "WHERE map_unit_id = ANY(:unit_ids) "
                "AND channel = ANY(:channels) "
                "AND token = ANY(:tokens)"
            ),
            {
                "unit_ids": map_unit_ids,
                "channels": list(_MAP_SCORE_CHANNELS),
                "tokens": query_tokens,
            },
        )
        frequencies: dict[tuple[str, str], dict[str, int]] = {}
        for map_unit_id, channel, token, frequency in frequency_result.all():
            frequencies.setdefault((str(map_unit_id), str(channel)), {})[str(token)] = (
                int(frequency)
            )

        path_stats = await _build_bm25_stats(
            db,
            unit_rows=unit_rows,
            map_unit_ids=map_unit_ids,
            channel="path",
            query_tokens=query_tokens,
            frequencies=frequencies,
            length_field="path_token_count",
        )
        content_stats = await _build_bm25_stats(
            db,
            unit_rows=unit_rows,
            map_unit_ids=map_unit_ids,
            channel="content",
            query_tokens=query_tokens,
            frequencies=frequencies,
            length_field="content_token_count",
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

        def _unit_has_query_hit(unit_id: str) -> bool:
            # BM25 fused score can be 0 when IDF is 0 (tiny corpus). A unit
            # that actually holds a query token is still a hit.
            return any(
                frequencies.get((unit_id, channel), {}).get(token, 0) > 0
                for channel in _MAP_SCORE_CHANNELS
                for token in query_tokens
            )

        ranked_unit_ids = sorted(
            (unit_id for unit_id in scores_by_unit if _unit_has_query_hit(unit_id)),
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
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(f"  search.map_unit_discovery failed: {exc}")
        return DiscoveryResult(status="error", error=str(exc), latency_ms=latency)


async def _build_bm25_stats(
    session: AsyncSession,
    *,
    unit_rows: list[dict[str, Any]],
    map_unit_ids: list[str],
    channel: str,
    query_tokens: list[str],
    frequencies: dict[tuple[str, str], dict[str, int]],
    length_field: str,
) -> PersistedBm25Stats:
    lengths = [
        int(row[length_field]) for row in unit_rows if int(row[length_field]) > 0
    ]
    document_count = len(lengths)
    document_frequency = {
        token: sum(
            1
            for row in unit_rows
            if frequencies.get((row["map_unit_id"], channel), {}).get(token, 0) > 0
        )
        for token in query_tokens
    }
    needs_average_idf = any(
        frequency > document_count / 2 for frequency in document_frequency.values()
    )
    average_idf = 0.0
    if needs_average_idf and map_unit_ids and document_count:
        result = await session.execute(
            text(
                "SELECT COALESCE(AVG(LN((:document_count - frequencies.document_frequency "
                "+ 0.5) / (frequencies.document_frequency + 0.5))), 0.0) "
                "FROM (SELECT token, COUNT(*) AS document_frequency "
                "FROM document_map_unit_tokens "
                "WHERE map_unit_id = ANY(:unit_ids) AND channel = :channel "
                "GROUP BY token) AS frequencies"
            ),
            {
                "document_count": document_count,
                "unit_ids": map_unit_ids,
                "channel": channel,
            },
        )
        row = result.first()
        average_idf = float(row[0]) if row else 0.0
    return PersistedBm25Stats(
        document_count=document_count,
        total_length=sum(lengths),
        document_frequency=document_frequency,
        average_idf=average_idf,
    )


def _as_metadata_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _primary_chunk(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for chunk in chunks:
        if normalize_chunk_type(chunk.get("chunk_type")) not in ASSET_CHUNK_TYPES:
            return chunk
    return chunks[0] if chunks else None


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
    """Map each winning unit to one chunk; asset-only requests follow connect_to."""
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
    chunk_rows_by_section: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for chunk_row in result.all():
        row = dict(chunk_row._mapping)
        row["chunk_metadata"] = _as_metadata_dict(row.get("chunk_metadata"))
        key = (row["document_id"], row["job_result_id"], row["section_id"])
        chunk_rows_by_section.setdefault(key, []).append(row)

    primaries: list[dict[str, Any]] = []
    for unit_id in ranked_unit_ids:
        unit_row = rows_by_unit_id[unit_id]
        key = (
            unit_row["document_id"],
            unit_row["job_result_id"],
            unit_row["section_id"],
        )
        primary = _primary_chunk(chunk_rows_by_section.get(key, []))
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
        candidates = list(chunk_rows_by_section.get(key, ()))
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
