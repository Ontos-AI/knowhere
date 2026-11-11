"""Visual page embedding discovery (pgvector cosine) and RRF hybrid fusion."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pgvector import HalfVector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.services.ai.embedding import embed_query
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.row_utils import (
    apply_matched_page_nums,
    coerce_page_nums,
    extract_matched_page_nums,
    is_unrefined_page,
    iter_connected_target_ids,
    normalize_chunk_type,
)
from shared.services.retrieval.scoring.knowhere_hybrid import (
    CHANNEL_WEIGHT_CONTENT,
    CHANNEL_WEIGHT_PATH,
    CHANNEL_WEIGHT_VISUAL,
    RRF_K,
)
from shared.services.retrieval.search.map_unit_discovery import (
    DiscoveryResult,
    _as_metadata_dict,
    _build_signal_clause,
    map_unit_discovery,
)
from shared.services.retrieval.search.scoring import normalize_row_scores
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES, DEFAULT_TOP_K
from shared.services.retrieval.required_query_embedding import RequiredQueryEmbeddingException

__all__ = [
    "hybrid_discovery",
    "light_unrefined_section_pages",
    "visual_page_discovery",
]


def _build_visual_revision_scope(
    revision_pins: Mapping[str, str] | None,
) -> tuple[str, str, dict[str, Any]]:
    if revision_pins is None:
        return ("AND d.current_job_result_id = dpe.job_result_id", "", {})
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
        document_key = f"_vpin_document_{index}"
        revision_key = f"_vpin_revision_{index}"
        placeholders.append(f"(:{document_key}, :{revision_key})")
        params[document_key] = document_id
        params[revision_key] = job_result_id
    return (
        "",
        f"AND (dpe.document_id, dpe.job_result_id) IN ({', '.join(placeholders)})",
        params,
    )


def _build_visual_section_subtree_clause(
    targets: list[tuple[str, str | None]] | None,
) -> tuple[str, dict[str, Any]]:
    if not targets or not any(section_path for _document_id, section_path in targets):
        return "", {}
    parts: list[str] = []
    params: dict[str, Any] = {}
    for index, (document_id, section_path) in enumerate(targets):
        doc_key = f"_vsec_doc_{index}"
        params[doc_key] = document_id
        if not section_path:
            parts.append(f"(dc.document_id = :{doc_key})")
            continue
        path_key = f"_vsec_path_{index}"
        like_key = f"_vsec_pathlike_{index}"
        params[path_key] = section_path
        params[like_key] = f"{section_path} / %"
        parts.append(
            f"(dc.document_id = :{doc_key} AND "
            f"(ds.section_path = :{path_key} OR ds.section_path LIKE :{like_key}))"
        )
    return f"AND ({' OR '.join(parts)})", params


def _build_visual_exclude_sections_clause(
    exclude_sections: list[dict[str, str]],
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    for index, item in enumerate(exclude_sections):
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        section_path = str(item.get("section_path") or "").strip()
        if not document_id or not section_path:
            continue
        doc_key = f"_vexc_doc_{index}"
        path_key = f"_vexc_path_{index}"
        like_key = f"_vexc_pathlike_{index}"
        params[doc_key] = document_id
        params[path_key] = section_path
        params[like_key] = f"{section_path} / %"
        parts.append(
            f"(dc.document_id = :{doc_key} AND "
            f"(ds.section_path = :{path_key} OR ds.section_path LIKE :{like_key}))"
        )
    if not parts:
        return "", {}
    return f"AND NOT ({' OR '.join(parts)})", params


def _aggregate_visual_page_hits(raw_rows: Sequence[Any]) -> list[dict[str, Any]]:
    primaries: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_row in raw_rows:
        row = dict(raw_row._mapping)
        similarity = max(0.0, float(row.pop("similarity", 0.0) or 0.0))
        page_nums = coerce_page_nums([row.pop("page_num", None)])
        key = (str(row.get("document_id") or ""), str(row.get("chunk_id") or ""))
        existing = groups.get(key)
        if existing is None:
            row["chunk_metadata"] = _as_metadata_dict(row.get("chunk_metadata"))
            row["score"] = similarity
            row["matched_page_nums"] = list(page_nums)
            groups[key] = row
            primaries.append(row)
            continue
        for page in page_nums:
            if page not in existing["matched_page_nums"]:
                existing["matched_page_nums"].append(page)
    return primaries


def _copy_matched_page_nums(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    pages = coerce_page_nums(source.get("matched_page_nums"))
    if pages:
        target["matched_page_nums"] = pages


class VisualQueryEmbedding:
    """Resolve one vector lazily for the visual searches in this request."""

    def __init__(self, query: str) -> None:
        self._query: str = query
        self._vector: list[float] | None = None

    async def get(self) -> list[float]:
        if self._vector is None:
            try:
                self._vector = await asyncio.to_thread(
                    embed_query, self._query, usage_task="retrieval.query_embed",
                )
            except Exception as exc:
                raise RequiredQueryEmbeddingException(
                    internal_message="Required retrieval query embedding failed",
                    provider="query_embedding", original_exception=exc,
                ) from exc
        return self._vector


@dataclass(frozen=True)
class _VisualDocumentScope:
    storage: CorpusStorage
    documents: DocumentScope
    revision_join: str
    revision_clause: str
    document_clause: str
    parameters: dict[str, Any]
    has_inline_build_pins: bool


def _resolve_visual_document_scope(
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    document_scope: DocumentScope,
    revision_pins: Mapping[str, str] | None,
) -> _VisualDocumentScope:
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead

    storage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    documents: DocumentScope = document_scope.excluding(exclude_document_ids)
    revision_join, revision_clause, revision_parameters = _build_visual_revision_scope(
        revision_pins
    )
    document_clause, document_parameters = documents.sql()
    native_read = PublishedLexicalRead.current()
    has_inline_build_pins: bool = False
    if (
        native_read is not None
        and native_read.scope.namespace == namespace
        and native_read.scope.ownerId == storage.resolve_owner(user_id)
        and native_read.manifestId is None
    ):
        has_inline_build_pins = True
        # Inline native builds can still be searched after PostgreSQL publishes
        # a replacement. Score visual pages only while their build is current.
        document_clause += """
            AND (d.document_id, d.current_indexing_build_id) IN (
                SELECT * FROM unnest(
                    CAST(:visual_document_ids AS varchar[]),
                    CAST(:visual_build_ids AS varchar[])
                )
            )
        """
        build_pins: dict[str, str] = {
            document_id: build_id
            for document_id, build_id in native_read.builds.items()
            if documents.allows(document_id)
        }
        document_parameters = {
            **document_parameters,
            "visual_document_ids": list(build_pins),
            "visual_build_ids": list(build_pins.values()),
        }
    return _VisualDocumentScope(
        storage=storage,
        documents=documents,
        revision_join=revision_join,
        revision_clause=revision_clause,
        document_clause=document_clause,
        parameters={
            "user_id": storage.resolve_owner(user_id),
            "namespace": namespace,
            **revision_parameters,
            **document_parameters,
        },
        has_inline_build_pins=has_inline_build_pins,
    )


def _can_search_visual_pages(
    query: str, top_k: int, chunk_types: set[str] | None,
    revision_pins: Mapping[str, str] | None,
) -> bool:
    return (
        bool(settings.VISUAL_RETRIEVAL_ENABLED)
        and bool(query.strip())
        and top_k > 0
        and (revision_pins is None or bool(revision_pins))
        and (
            chunk_types is None
            or "page" in chunk_types
            or chunk_types.issubset(ASSET_CHUNK_TYPES)
        )
    )


async def _has_visual_embeddings(db: AsyncSession, scope: _VisualDocumentScope) -> bool:
    statement: str = f"""
        SELECT EXISTS (
            SELECT 1
            FROM document_page_embeddings dpe
            JOIN documents d
                ON d.document_id = dpe.document_id
                {scope.revision_join}
            WHERE d.user_id = :user_id
                AND d.namespace = :namespace
                AND d.status = 'active'
                {scope.revision_clause}
                {scope.document_clause}
        )
    """
    result = await db.execute(text(scope.storage.compile_sql(statement)), scope.parameters)
    return bool(result.scalar())


async def has_visual_query_embeddings(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    document_scope: DocumentScope,
    chunk_types: set[str] | None,
    revision_pins: Mapping[str, str] | None,
) -> bool:
    """Check whether the visual channel needs a query vector in this read view."""
    if not _can_search_visual_pages(query, top_k, chunk_types, revision_pins):
        return False
    return await _has_visual_embeddings(
        db,
        _resolve_visual_document_scope(
            user_id=user_id,
            namespace=namespace,
            exclude_document_ids=exclude_document_ids,
            document_scope=document_scope,
            revision_pins=revision_pins,
        ),
    )


async def visual_page_discovery(
    db: AsyncSession | None,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str] | None = None,
    exclude_sections: list[dict[str, str]] | None = None,
    document_scope: DocumentScope = DocumentScope(),
    chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    revision_pins: Mapping[str, str] | None = None,
    section_targets: list[tuple[str, str | None]] | None = None,
    query_embedding: VisualQueryEmbedding | None = None,
) -> DiscoveryResult:
    """Rank persisted page embeddings via pgvector cosine similarity."""
    if db is None:
        raise ValueError("database session required for visual_page_discovery")
    normalized_query = str(query or "").strip()
    if not _can_search_visual_pages(normalized_query, top_k, chunk_types, revision_pins):
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    t0 = time.monotonic()
    exclude_doc_list = list(exclude_document_ids or [])
    exclude_sec_list = list(exclude_sections or [])
    scope: _VisualDocumentScope = _resolve_visual_document_scope(
        user_id=user_id,
        namespace=namespace,
        exclude_document_ids=exclude_doc_list,
        document_scope=document_scope,
        revision_pins=revision_pins,
    )
    if not await _has_visual_embeddings(db, scope):
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    embedding = query_embedding or VisualQueryEmbedding(normalized_query)
    vector_literal = HalfVector(await embedding.get()).to_text()

    signal_clause, signal_params = _build_signal_clause(
        signal_paths or [], filter_mode
    )
    section_clause, section_params = _build_visual_section_subtree_clause(
        section_targets
    )
    exclude_clause, exclude_params = _build_visual_exclude_sections_clause(
        exclude_sec_list
    )
    query_params: dict[str, Any] = {
        **scope.parameters,
        **signal_params,
        **section_params,
        **exclude_params,
        "query_embedding": vector_literal,
        "top_k": int(top_k),
    }

    search_sql = f"""
        WITH scoped_chunks AS MATERIALIZED (
            SELECT
                dc.chunk_id,
                dc.document_id,
                dc.section_id,
                dc.chunk_type,
                dc.content,
                dc.source_chunk_path,
                dc.file_path,
                dc.chunk_metadata,
                dc.job_result_id,
                dc.sort_order,
                d.current_indexing_build_id AS visual_build_id,
                ds.section_path,
                d.source_file_name,
                jr.job_id,
                jr.document_metadata ->> 'result_raw_prefix' AS result_raw_prefix
            FROM documents d
            JOIN document_chunks dc
                ON dc.document_id = d.document_id
                AND dc.chunk_type = 'page'
            LEFT JOIN document_sections ds
                ON ds.section_id = dc.section_id
            LEFT JOIN job_results jr
                ON jr.id = dc.job_result_id
            WHERE d.user_id = :user_id
                AND d.namespace = :namespace
                AND d.status = 'active'
                { "AND d.current_job_result_id = dc.job_result_id" if revision_pins is None else scope.revision_clause.replace("dpe.", "dc.") }
                {scope.document_clause}
                {signal_clause}
                {section_clause}
                {exclude_clause}
        ),
        scoped_chunk_pages AS MATERIALIZED (
            SELECT
                sc.chunk_id,
                sc.document_id,
                sc.job_result_id,
                CAST(p.val AS integer) AS page_num
            FROM scoped_chunks sc
            CROSS JOIN LATERAL json_array_elements_text(
                CASE
                    WHEN json_typeof(sc.chunk_metadata -> 'page_nums') = 'array'
                    THEN sc.chunk_metadata -> 'page_nums'
                    ELSE '[]'::json
                END
            ) AS p(val)
            WHERE p.val ~ '^[0-9]+$'
        ),
        top_pages AS MATERIALIZED (
            SELECT
                dpe.document_id,
                dpe.job_result_id,
                dpe.page_num,
                (dpe.embedding <=> CAST(:query_embedding AS halfvec)) AS distance,
                1.0 - (dpe.embedding <=> CAST(:query_embedding AS halfvec))
                    AS similarity
            FROM document_page_embeddings dpe
            WHERE EXISTS (
                SELECT 1
                FROM scoped_chunk_pages scp
                WHERE scp.document_id = dpe.document_id
                    AND scp.job_result_id = dpe.job_result_id
                    AND scp.page_num = dpe.page_num
            )
            ORDER BY distance ASC,
                     dpe.page_num ASC
            LIMIT :top_k
        )
        SELECT
            tp.page_num,
            tp.similarity,
            sc.chunk_id,
            sc.document_id,
            sc.section_id,
            sc.chunk_type,
            sc.content,
            sc.source_chunk_path,
            sc.file_path,
            sc.chunk_metadata,
            sc.job_result_id,
            sc.sort_order,
            sc.visual_build_id,
            sc.section_path,
            sc.source_file_name,
            sc.job_id,
            sc.result_raw_prefix
        FROM top_pages tp
        JOIN scoped_chunk_pages scp
            ON scp.document_id = tp.document_id
            AND scp.job_result_id = tp.job_result_id
            AND scp.page_num = tp.page_num
        JOIN scoped_chunks sc
            ON sc.document_id = scp.document_id
            AND sc.job_result_id = scp.job_result_id
            AND sc.chunk_id = scp.chunk_id
        ORDER BY tp.distance ASC,
                 tp.page_num ASC,
                 sc.sort_order ASC,
                 sc.chunk_id ASC
    """
    result = await db.execute(
        text(scope.storage.compile_sql(search_sql)), query_params
    )

    primaries = _aggregate_visual_page_hits(result.all())
    if scope.has_inline_build_pins:
        for primary in primaries:
            primary["build_id"] = primary["visual_build_id"]
            primary["uses_inline_build"] = True

    if chunk_types is not None and chunk_types.issubset(ASSET_CHUNK_TYPES):
        connected = await hydrate_connected_target_rows(
            db=db,
            rows=primaries,
            exclude_document_ids=exclude_doc_list,
            exclude_sections=exclude_sec_list,
            document_scope=scope.documents,
            revision_pins=revision_pins,
        )
        connected_by_id = {
            (row.get("build_id"), str(row["chunk_id"])): row
            for row in connected
            if row.get("chunk_id")
        }
        asset_rows: list[dict[str, Any]] = []
        seen_ids: set[tuple[str | None, str]] = set()
        for primary in primaries:
            score = float(primary.get("score") or 0.0)
            for target_id in iter_connected_target_ids(primary):
                key: tuple[str | None, str] = (primary.get("build_id"), target_id)
                target = connected_by_id.get(key)
                if target is None:
                    continue
                if (
                    key in seen_ids
                    or normalize_chunk_type(target.get("chunk_type")) not in chunk_types
                ):
                    continue
                seen_ids.add(key)
                item = dict(target)
                item["score"] = score
                if scope.has_inline_build_pins:
                    item["visual_build_id"] = primary["build_id"]
                    item["uses_inline_build"] = True
                asset_rows.append(item)
        fused_rows = asset_rows[:top_k]
    else:
        fused_rows = primaries

    if fused_rows:
        normalize_row_scores(
            fused_rows,
            source_field="score",
            target_field="discovery_score",
            default=0.5,
        )
    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        "  search.visual_page_discovery: {} fused rows, {}ms",
        len(fused_rows),
        latency,
    )
    return DiscoveryResult(
        status="discovery_done",
        payload={"fused_rows": fused_rows},
        latency_ms=latency,
    )


async def light_unrefined_section_pages(
    db: AsyncSession | None,
    *,
    user_id: str,
    namespace: str,
    query: str,
    rows: list[dict[str, Any]],
    document_scope: DocumentScope = DocumentScope(),
    revision_pins: Mapping[str, str] | None = None,
    query_embedding: VisualQueryEmbedding | None = None,
) -> None:
    """Populate ``matched_page_nums`` on unrefined page rows via section-scoped visual discovery."""
    if (
        db is None
        or not rows
        or not str(query or "").strip()
        or not bool(getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True))
    ):
        return

    pending: list[dict[str, Any]] = []
    targets: list[tuple[str, str]] = []
    seen_targets: set[tuple[str, str]] = set()
    for row in rows:
        if normalize_chunk_type(row.get("chunk_type")) != "page":
            continue
        if not is_unrefined_page(row):
            continue
        if extract_matched_page_nums(row):
            continue
        document_id = str(row.get("document_id") or "").strip()
        section_path = str(row.get("section_path") or "").strip()
        if not document_id or not section_path:
            continue
        pending.append(row)
        target = (document_id, section_path)
        if target not in seen_targets:
            seen_targets.add(target)
            targets.append(target)

    if not pending or not targets:
        return

    embedding = query_embedding or VisualQueryEmbedding(str(query or '').strip())
    pages_by_chunk: dict[tuple[str, str], tuple[str | None, list[int]]] = {}
    for document_id, section_path in targets:
        discovery = await visual_page_discovery(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=DEFAULT_TOP_K,
            document_scope=document_scope,
            revision_pins=revision_pins,
            section_targets=[(document_id, section_path)],
            query_embedding=embedding,
        )
        for vis_row in discovery.payload.get("fused_rows") or []:
            if not isinstance(vis_row, dict):
                continue
            doc_id = str(vis_row.get("document_id") or "").strip()
            chunk_id = str(vis_row.get("chunk_id") or "").strip()
            pages = coerce_page_nums(vis_row.get("matched_page_nums"))
            if doc_id and chunk_id and pages:
                pages_by_chunk[(doc_id, chunk_id)] = (vis_row.get("visual_build_id"), pages)

    for row in pending:
        key = (
            str(row.get("document_id") or "").strip(),
            str(row.get("chunk_id") or "").strip(),
        )
        match = pages_by_chunk.get(key)
        if match is None:
            continue
        visual_build_id, pages = match
        if row.get("build_id") is None or row["build_id"] == visual_build_id:
            apply_matched_page_nums(row, pages)


async def hybrid_discovery(
    db: AsyncSession | None,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str] | None = None,
    exclude_sections: list[dict[str, str]] | None = None,
    document_scope: DocumentScope = DocumentScope(),
    chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    revision_pins: Mapping[str, str] | None = None,
    publish_index_readiness: bool = True,
    section_targets: list[tuple[str, str | None]] | None = None,
    use_visual: bool = True,
    query_embedding: VisualQueryEmbedding | None = None,
) -> DiscoveryResult:
    """Fuse persisted lexical map-unit BM25 discovery with visual page discovery via RRF."""
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead

    exclude_doc_list = list(exclude_document_ids or [])
    exclude_sec_list = list(exclude_sections or [])

    lexical_result = await map_unit_discovery(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_doc_list,
        exclude_sections=exclude_sec_list,
        document_scope=document_scope,
        chunk_types=chunk_types,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        revision_pins=revision_pins,
        publish_index_readiness=publish_index_readiness,
        section_targets=section_targets,
    )
    if (
        not use_visual
        or not settings.VISUAL_RETRIEVAL_ENABLED
        or getattr(PublishedLexicalRead.current(), "manifestId", None) is not None
    ):
        return lexical_result

    embedding = query_embedding or VisualQueryEmbedding(str(query or '').strip())
    visual_result = await visual_page_discovery(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        top_k=top_k,
        exclude_document_ids=exclude_doc_list,
        exclude_sections=exclude_sec_list,
        document_scope=document_scope,
        chunk_types=chunk_types,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        revision_pins=revision_pins,
        section_targets=section_targets,
        query_embedding=embedding,
    )

    lexical_rows = list(lexical_result.payload.get("fused_rows") or [])
    lexical_builds: dict[str, str] = {
        row["document_id"]: row["build_id"]
        for row in lexical_rows
        if row.get("build_id")
    }
    visual_rows = [
        row for row in visual_result.payload.get("fused_rows") or []
        if row.get("document_id") not in lexical_builds
        or row.get("visual_build_id") == lexical_builds[row["document_id"]]
    ]

    if not visual_rows:
        return lexical_result
    if not lexical_rows:
        visual_result.has_unhydrated_hits = lexical_result.has_unhydrated_hits
        return visual_result

    lexical_weight = CHANNEL_WEIGHT_PATH + CHANNEL_WEIGHT_CONTENT
    rrf_scores: dict[tuple[str, str], float] = {}
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for rank, row in enumerate(lexical_rows):
        key = (str(row.get("document_id") or ""), str(row.get("chunk_id") or ""))
        rows_by_key[key] = dict(row)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + lexical_weight / (
            RRF_K + rank + 1
        )

    for rank, row in enumerate(visual_rows):
        key = (str(row.get("document_id") or ""), str(row.get("chunk_id") or ""))
        if key not in rows_by_key:
            rows_by_key[key] = dict(row)
        else:
            _copy_matched_page_nums(rows_by_key[key], row)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + CHANNEL_WEIGHT_VISUAL / (
            RRF_K + rank + 1
        )

    ranked_keys = sorted(
        rrf_scores,
        key=lambda key: rrf_scores[key],
        reverse=True,
    )[:top_k]

    fused_rows: list[dict[str, Any]] = []
    for key in ranked_keys:
        merged = dict(rows_by_key[key])
        merged["score"] = rrf_scores[key]
        fused_rows.append(merged)

    if fused_rows:
        await light_unrefined_section_pages(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            rows=fused_rows,
            document_scope=document_scope,
            revision_pins=revision_pins,
            query_embedding=embedding,
        )
        normalize_row_scores(
            fused_rows,
            source_field="score",
            target_field="discovery_score",
            default=0.5,
        )

    return DiscoveryResult(
        status="discovery_done",
        payload={"fused_rows": fused_rows},
        latency_ms=int(lexical_result.latency_ms or 0)
        + int(visual_result.latency_ms or 0),
        has_unhydrated_hits=lexical_result.has_unhydrated_hits,
    )
