from __future__ import annotations

import os
import time
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.cache_service import get_cached_retrieval_query_result, set_cached_retrieval_query_result
from shared.services.retrieval.channels import path_channel, content_channel, term_channel
from shared.services.retrieval.graph_service import GraphQueryService
from shared.services.retrieval.hit_stats_recorder import schedule_retrieval_hit_stats_update
from shared.services.retrieval.hydration import (
    assemble_retrieval_results,
    hydrate_referenced_chunk_rows,
)
from shared.services.retrieval.response_projection import (
    attach_citation,
    enrich_referenced_chunks_with_asset_urls,
    project_public_retrieval_response,
)
from shared.services.retrieval.scoring import (
    get_row_path,
    merge_channels_rrf,
    merge_same_section_rows,
    normalize_row_scores,
)
from shared.services.retrieval.ranking import rank_retrieval_candidates
from shared.services.retrieval.scoped_corpus import count_scoped_chunks, load_all_scoped_chunks
from shared.services.retrieval.settings import (
    CHANNEL_WEIGHT_CONTENT as _CHANNEL_WEIGHT_CONTENT,
    CHANNEL_WEIGHT_PATH as _CHANNEL_WEIGHT_PATH,
    CHANNEL_WEIGHT_TERM as _CHANNEL_WEIGHT_TERM,
    INTERNAL_RECALL_K_MULTIPLIER as _INTERNAL_RECALL_K_MULTIPLIER,
    resolve_allowed_chunk_types as _resolve_allowed_chunk_types,
)


async def list_graph_routed_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    service = GraphQueryService()
    entry_document_ids = await service.find_entry_documents(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    return await service.collect_candidate_chunks(
        db,
        user_id=user_id,
        namespace=namespace,
        entry_document_ids=entry_document_ids,
        query=query,
        top_k=top_k * _INTERNAL_RECALL_K_MULTIPLIER,
        exclude_sections=exclude_sections,
    )


async def run_retrieval_query(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    data_type: int = 1,
    signal_paths: list[str] | None = None,
    filter_mode: str = 'delete',
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
    use_agentic: bool | None = None,
) -> dict[str, Any]:
    """Checkerboard retrieval: 3 independent channels -> RRF -> agent/graph union -> assembly."""
    t_start = time.monotonic()
    query = query.strip()
    logger.info('\n' + '█' * 70)
    logger.info('  🚀 RETRIEVAL PIPELINE START')
    logger.info(f'  query="{query}"')
    logger.info(f'  user={user_id}  ns={namespace}  top_k={top_k}  data_type={data_type}')
    logger.info(f'  exclude_docs={exclude_document_ids}  exclude_secs={len(exclude_sections)}')
    logger.info('█' * 70)

    if not query:
        logger.info('  ⛔ Empty query filtered, skipping retrieval pipeline')
        return {
            "namespace": namespace,
            "query": query,
            "router_used": "empty_query_filtered",
            "results": [],
        }

    allowed_chunk_types = _resolve_allowed_chunk_types(data_type)
    effective_recall_k = internal_recall_k if internal_recall_k is not None else top_k * _INTERNAL_RECALL_K_MULTIPLIER
    logger.info(f'  allowed_chunk_types={allowed_chunk_types}  effective_recall_k={effective_recall_k}  signal_paths={signal_paths}  filter_mode={filter_mode}  rerank={rerank}  threshold={threshold}')

    cache_extra = dict(
        data_type=data_type,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        channels=channels,
        channel_weights=channel_weights,
        rerank=rerank,
        threshold=threshold,
        internal_recall_k=internal_recall_k,
        # Always True: agentic mode now always routes through workflow
        decomposition_enabled=True,
    )

    cache_version: int | None = None
    try:
        cache_version, cached = await get_cached_retrieval_query_result(
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            **cache_extra,
        )
        if cached:
            logger.info(f'retrieval: cache_hit=True version={cache_version}')
            try:
                schedule_retrieval_hit_stats_update(
                    user_id=user_id,
                    namespace=namespace,
                    results=cached.get("results", []),
                )
            except Exception as e:
                logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")
            return await project_public_retrieval_response(cached)
    except Exception as e:
        logger.warning(f"Failed to read retrieval cache (ignored): {e}")

    logger.debug(f'  📦 Cache miss (version={cache_version}), running full pipeline')

    # ── Small KB optimization ──
    try:
        total_chunk_count = await count_scoped_chunks(
            db, user_id=user_id, namespace=namespace,
            exclude_document_ids=exclude_document_ids,
            allowed_chunk_types=allowed_chunk_types,
        )
    except Exception as e:
        logger.warning(f"Failed to count scoped chunks, skipping small KB optimization: {e}")
        total_chunk_count = top_k + 1
    logger.info(f'\n  📊 Total chunks in scope: {total_chunk_count}')
    if total_chunk_count <= top_k:
        logger.info(f'  ⚡ Small KB optimization: {total_chunk_count} chunks <= top_k={top_k}, returning all')
        all_rows = await load_all_scoped_chunks(
            db, user_id=user_id, namespace=namespace,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            allowed_chunk_types=allowed_chunk_types,
            signal_paths=signal_paths or [],
            filter_mode=filter_mode,
        )
        logger.info(f'  small_kb load: loaded={len(all_rows)} rows after signal/exclude filters')
        assembled_rows = await assemble_retrieval_results(
            db=db, rows=all_rows,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            allowed_chunk_types=allowed_chunk_types,
        )
        results = [attach_citation(row) for row in assembled_rows]
        response = {
            "namespace": namespace, "query": query,
            "router_used": "small_kb_all", "results": results,
        }
        if cache_version is not None:
            try:
                await set_cached_retrieval_query_result(
                    user_id=user_id, namespace=namespace, version=cache_version,
                    query=query, top_k=top_k,
                    exclude_document_ids=exclude_document_ids,
                    exclude_sections=exclude_sections,
                    response=response, **cache_extra,
                )
            except Exception as e:
                logger.warning(f"Failed to write retrieval cache (ignored): {e}")
        try:
            schedule_retrieval_hit_stats_update(user_id=user_id, namespace=namespace, results=results)
        except Exception as e:
            logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")
        elapsed_total = round((time.monotonic() - t_start) * 1000)
        logger.info(f'  ✅ Small KB: {len(results)} results in {elapsed_total}ms')
        return await project_public_retrieval_response(response)

    # ══ Route: agentic (unified workflow) vs legacy ══
    if use_agentic is not None:
        _agentic_enabled = use_agentic
    else:
        _agentic_enabled = os.environ.get('RETRIEVAL_AGENTIC_ENABLED', 'true') == 'true'
    if _agentic_enabled:
        # ── Unified agentic path via WorkflowOrchestrator ──
        # Simple queries: planner returns a single-step plan (no decomposition).
        # Complex queries: planner returns a multi-step plan with synthesize.
        # Both go through the same code path.
        from shared.services.retrieval.workflow.orchestrator import WorkflowOrchestrator

        workflow = WorkflowOrchestrator()
        workflow_result = await workflow.run(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            data_type=data_type,
            signal_paths=signal_paths,
            filter_mode=filter_mode,
            channels=channels,
            channel_weights=channel_weights,
        )

        enriched_refs = await enrich_referenced_chunks_with_asset_urls(
            workflow_result.referenced_chunks,
        )

        workflow_result_rows = await hydrate_referenced_chunk_rows(
            db=db,
            user_id=user_id,
            namespace=namespace,
            refs=enriched_refs,
        )
        scoped_reference_keys = {
            (
                str(row.get('document_id') or '').strip(),
                str(row.get('chunk_id') or '').strip(),
            )
            for row in workflow_result_rows
        }
        enriched_refs = [
            ref for ref in enriched_refs
            if (
                str(ref.get('document_id') or '').strip(),
                str(ref.get('chunk_id') or '').strip(),
            ) in scoped_reference_keys
        ]
        assembled_workflow_rows = await assemble_retrieval_results(
            db=db,
            rows=workflow_result_rows,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            allowed_chunk_types=allowed_chunk_types,
        )
        response = workflow_result.to_api_response()
        # Override referenced_chunks with enriched versions
        response['referenced_chunks'] = enriched_refs
        response['results'] = [attach_citation(row) for row in assembled_workflow_rows]

        if cache_version is not None:
            try:
                await set_cached_retrieval_query_result(
                    user_id=user_id, namespace=namespace, version=cache_version,
                    query=query, top_k=top_k,
                    exclude_document_ids=exclude_document_ids,
                    exclude_sections=exclude_sections,
                    response=response, **cache_extra,
                )
            except Exception as e:
                logger.warning(f"Failed to write retrieval cache (ignored): {e}")

        try:
            schedule_retrieval_hit_stats_update(
                user_id=user_id, namespace=namespace,
                results=enriched_refs,
            )
        except Exception as e:
            logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")

        elapsed_total = round((time.monotonic() - t_start) * 1000)
        logger.info(
            f'\n{"█" * 70}\n'
            f'  ✅ AGENTIC RETRIEVAL COMPLETE: '
            f'{len(enriched_refs)} chunks | '
            f'answer={len(workflow_result.answer_text)} chars | '
            f'router={workflow_result.router_used} | {elapsed_total}ms\n'
            f'{"█" * 70}'
        )

        return await project_public_retrieval_response(response)

    else:

        # ── LEGACY path (existing code, unchanged) ──

        # ── Channel execution ──
        active_channels = set(channels) if channels else {'path', 'content', 'term'}
        logger.info(f'\n  📡 PHASE 1: Bottom-Layer Discovery (channels={sorted(active_channels)})')
        logger.info(f'  effective_recall_k={effective_recall_k}')

        path_rows: list[dict[str, Any]] = []
        content_rows: list[dict[str, Any]] = []
        term_rows: list[dict[str, Any]] = []

        if 'path' in active_channels:
            t_ch = time.monotonic()
            path_rows = await path_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 path_channel: {len(path_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(path_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  type={r.get("chunk_type","?")}')
            if len(path_rows) > 5:
                logger.info(f'    ... and {len(path_rows) - 5} more')

        if 'content' in active_channels:
            t_ch = time.monotonic()
            content_rows = await content_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 content_channel: {len(content_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(content_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  content={str(r.get("content",""))[:80]}')
            if len(content_rows) > 5:
                logger.info(f'    ... and {len(content_rows) - 5} more')

        if 'term' in active_channels:
            t_ch = time.monotonic()
            term_rows = await term_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 term_channel: {len(term_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(term_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  type={r.get("chunk_type","?")}')
            if len(term_rows) > 5:
                logger.info(f'    ... and {len(term_rows) - 5} more')

        # ── RRF fusion with configurable weights ──
        default_weights = {
            'path': _CHANNEL_WEIGHT_PATH,
            'content': _CHANNEL_WEIGHT_CONTENT,
            'term': _CHANNEL_WEIGHT_TERM,
        }
        effective_weights = {**default_weights, **(channel_weights or {})}

        channel_lists: list[list[dict[str, Any]]] = []
        weight_list: list[float] = []

        if path_rows:
            channel_lists.append(path_rows)
            weight_list.append(effective_weights.get('path', _CHANNEL_WEIGHT_PATH))
        if content_rows:
            channel_lists.append(content_rows)
            weight_list.append(effective_weights.get('content', _CHANNEL_WEIGHT_CONTENT))
        if term_rows:
            channel_lists.append(term_rows)
            weight_list.append(effective_weights.get('term', _CHANNEL_WEIGHT_TERM))

        fused_rows = merge_channels_rrf(channel_lists, weight_list, effective_recall_k) if channel_lists else []
        logger.info(f'\n  🔀 RRF Fusion: {len(fused_rows)} rows from {len(channel_lists)} channels (weights={dict(zip(["path","content","term"][:len(weight_list)], weight_list))})')
        for i, r in enumerate(fused_rows[:5]):
            logger.info(f'    [{i}] rrf_score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}')
        if len(fused_rows) > 5:
            logger.info(f'    ... and {len(fused_rows) - 5} more')

        # ── Section merging ──
        pre_merge = len(fused_rows)
        fused_rows = merge_same_section_rows(fused_rows)
        if len(fused_rows) != pre_merge:
            logger.info(f'retrieval: section_merge={pre_merge}->{len(fused_rows)}')

        # ── Threshold filtering ──
        if threshold > 0.0 and fused_rows:
            pre_count = len(fused_rows)
            fused_rows = [row for row in fused_rows if row.get('score', 0.0) >= threshold]
            logger.info(f'retrieval: threshold_filter={pre_count}->{len(fused_rows)} (threshold={threshold})')

        if fused_rows:
            normalize_row_scores(
                fused_rows,
                source_field='score',
                target_field='discovery_score',
                default=0.5,
            )

        # ── Legacy graph routing ──
        logger.info('\n  🧭 PHASE 2: Legacy Graph Routing')
        router_used = 'discovery_only'
        agent_rows: list[dict[str, Any]] = []

        try:
            agent_rows = await list_graph_routed_chunks(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=top_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
            )
            if agent_rows:
                router_used = 'discovery+graph'
                logger.info(f'  📊 Graph routing: {len(agent_rows)} rows')
        except Exception as exc:
            logger.error(f'  ❌ Graph routing failed (ignored): {exc}')
            agent_rows = []

        if agent_rows:
            normalize_row_scores(
                agent_rows,
                source_field='score',
                target_field='agent_score',
                default=0.5,
            )

        ranked_rows = await rank_retrieval_candidates(
            db,
            user_id=user_id,
            namespace=namespace,
            discovery_rows=fused_rows,
            routed_rows=agent_rows,
            top_k=top_k,
        )
        if ranked_rows:
            logger.info(f'\n  🧮 Unified candidate ranking: {len(ranked_rows)} rows')
            for i, row in enumerate(ranked_rows[:10]):
                logger.info(
                    '    '
                    f'[{i}] evidence={row.get("evidence_score", 0.0):.4f} '
                    f'discovery={row.get("discovery_score", 0.0):.4f} '
                    f'agent={row.get("agent_score", 0.0):.4f} '
                    f'path={get_row_path(row)}'
                )

    assembled_rows = await assemble_retrieval_results(
        db=db,
        rows=ranked_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        allowed_chunk_types=allowed_chunk_types,
    )
    results = [attach_citation(row) for row in assembled_rows]

    response = {
        "namespace": namespace,
        "query": query,
        "router_used": router_used,
        "results": results,
    }

    if cache_version is not None:
        try:
            await set_cached_retrieval_query_result(
                user_id=user_id,
                namespace=namespace,
                version=cache_version,
                query=query,
                top_k=top_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                response=response,
                **cache_extra,
            )
        except Exception as e:
            logger.warning(f"Failed to write retrieval cache (ignored): {e}")

    try:
        schedule_retrieval_hit_stats_update(
            user_id=user_id,
            namespace=namespace,
            results=results,
        )
    except Exception as e:
        logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")

    elapsed_total = round((time.monotonic() - t_start) * 1000)
    logger.info(f'\n{"█" * 70}')
    logger.info(f'  ✅ RETRIEVAL COMPLETE: {len(results)} results | router={router_used} | {elapsed_total}ms')
    for i, r in enumerate(results[:10]):
        src = r.get('source', {})
        logger.info(
            f'    [{i+1}] type={r.get("chunk_type","?")}  score={r.get("score",0):.4f}'
            f'  path={src.get("section_path","")}'
            f'  file={src.get("source_file_name","")}'
        )
    if len(results) > 10:
        logger.info(f'    ... and {len(results) - 10} more')
    logger.info(f'{"█" * 70}')

    return await project_public_retrieval_response(response)
