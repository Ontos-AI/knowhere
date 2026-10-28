"""Visual page embedding discovery (pgvector cosine) and RRF hybrid fusion."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
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
            parts.append(f"(dpe.document_id = :{doc_key})")
            continue
        path_key = f"_vsec_path_{index}"
        like_key = f"_vsec_pathlike_{index}"
        params[path_key] = section_path
        params[like_key] = f"{section_path} / %"
        parts.append(
            f"(dpe.document_id = :{doc_key} AND "
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
            f"(dpe.document_id = :{doc_key} AND "
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
) -> DiscoveryResult:
    """Rank persisted page embeddings via pgvector cosine similarity."""
    if db is None:
        raise ValueError("database session required for visual_page_discovery")
    if not bool(getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)):
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})
    normalized_query = str(query or "").strip()
    if not normalized_query or top_k <= 0:
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})
    if revision_pins is not None and not revision_pins:
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    if (
        chunk_types is not None
        and "page" not in chunk_types
        and not chunk_types.issubset(ASSET_CHUNK_TYPES)
    ):
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    t0 = time.monotonic()
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    exclude_doc_list = list(exclude_document_ids or [])
    exclude_sec_list = list(exclude_sections or [])

    revision_join, revision_clause, revision_params = _build_visual_revision_scope(
        revision_pins
    )
    scoped_docs = document_scope.excluding(exclude_doc_list)
    document_scope_clause, document_scope_params = scoped_docs.sql()
    params: dict[str, Any] = {
        "user_id": corpusStorage.resolve_owner(user_id),
        "namespace": namespace,
        **revision_params,
        **document_scope_params,
    }

    exists_sql = f"""
        SELECT EXISTS (
            SELECT 1
            FROM document_page_embeddings dpe
            JOIN documents d
                ON d.document_id = dpe.document_id
                {revision_join}
            WHERE d.user_id = :user_id
                AND d.namespace = :namespace
                AND d.status = 'active'
                {revision_clause}
                {document_scope_clause}
        )
    """
    has_embeddings = bool(
        (
            await db.execute(text(corpusStorage.compile_sql(exists_sql)), params)
        ).scalar()
    )
    if not has_embeddings:
        return DiscoveryResult(status="discovery_done", payload={"fused_rows": []})

    try:
        query_vector = await asyncio.to_thread(
            embed_query, normalized_query, usage_task="retrieval.query_embed",
        )
    except Exception as exc:
        raise RequiredQueryEmbeddingException(
            internal_message="Required retrieval query embedding failed",
            provider="query_embedding", original_exception=exc,
        ) from exc
    vector_literal = HalfVector(query_vector).to_text()

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
        **params,
        **signal_params,
        **section_params,
        **exclude_params,
        "query_embedding": vector_literal,
        "top_k": int(top_k),
    }

    search_sql = f"""
        SELECT
            dpe.page_num,
            1.0 - (dpe.embedding <=> CAST(:query_embedding AS halfvec))
                AS similarity,
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
            ds.section_path,
            d.source_file_name,
            jr.job_id,
            jr.document_metadata ->> 'result_raw_prefix' AS result_raw_prefix
        FROM document_page_embeddings dpe
        JOIN documents d
            ON d.document_id = dpe.document_id
            {revision_join}
        JOIN document_chunks dc
            ON dc.document_id = dpe.document_id
            AND dc.job_result_id = dpe.job_result_id
            AND dc.chunk_id = dpe.chunk_id
        LEFT JOIN document_sections ds
            ON ds.section_id = dc.section_id
        LEFT JOIN job_results jr
            ON jr.id = dc.job_result_id
        WHERE d.user_id = :user_id
            AND d.namespace = :namespace
            AND d.status = 'active'
            {revision_clause}
            {document_scope_clause}
            {signal_clause}
            {section_clause}
            {exclude_clause}
        ORDER BY dpe.embedding <=> CAST(:query_embedding AS halfvec) ASC,
                 dpe.page_num ASC
        LIMIT :top_k
    """
    result = await db.execute(
        text(corpusStorage.compile_sql(search_sql)), query_params
    )

    primaries = _aggregate_visual_page_hits(result.all())

    if chunk_types is not None and chunk_types.issubset(ASSET_CHUNK_TYPES):
        connected = await hydrate_connected_target_rows(
            db=db,
            rows=primaries,
            exclude_document_ids=exclude_doc_list,
            exclude_sections=exclude_sec_list,
            document_scope=scoped_docs,
            revision_pins=revision_pins,
        )
        connected_by_id = {
            str(row.get("chunk_id") or ""): row
            for row in connected
            if row.get("chunk_id")
        }
        asset_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for primary in primaries:
            score = float(primary.get("score") or 0.0)
            for target_id in iter_connected_target_ids(primary):
                target = connected_by_id.get(target_id)
                if target is None:
                    continue
                cid = str(target.get("chunk_id") or "")
                if (
                    not cid
                    or cid in seen_ids
                    or normalize_chunk_type(target.get("chunk_type")) not in chunk_types
                ):
                    continue
                seen_ids.add(cid)
                item = dict(target)
                item["score"] = score
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

    pages_by_chunk: dict[tuple[str, str], list[int]] = {}
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
        )
        for vis_row in discovery.payload.get("fused_rows") or []:
            if not isinstance(vis_row, dict):
                continue
            doc_id = str(vis_row.get("document_id") or "").strip()
            chunk_id = str(vis_row.get("chunk_id") or "").strip()
            pages = coerce_page_nums(vis_row.get("matched_page_nums"))
            if doc_id and chunk_id and pages:
                pages_by_chunk[(doc_id, chunk_id)] = pages

    for row in pending:
        key = (
            str(row.get("document_id") or "").strip(),
            str(row.get("chunk_id") or "").strip(),
        )
        pages = pages_by_chunk.get(key)
        if pages:
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
) -> DiscoveryResult:
    """Fuse persisted lexical map-unit BM25 discovery with visual page discovery via RRF."""
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
    if not bool(getattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)):
        return lexical_result

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
    )

    lexical_rows = list(lexical_result.payload.get("fused_rows") or [])
    visual_rows = list(visual_result.payload.get("fused_rows") or [])

    if not visual_rows:
        return lexical_result
    if not lexical_rows:
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
    )
