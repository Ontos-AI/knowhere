from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.cache_service import (
    get_cached_retrieval_query_result,
    set_cached_retrieval_query_result,
)
from shared.services.retrieval.execution.routes import run_retrieval_route
from shared.services.retrieval.stats.recorder import (
    schedule_retrieval_hit_stats_update,
)
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.execution.query_request import RetrievalQuery


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
    filter_mode: str = "delete",
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
    use_agentic: bool | None = None,
) -> dict[str, Any]:
    """Run retrieval through the plan module."""
    return await RetrievalExecutionPlan(
        RetrievalQuery.from_parameters(
            db=db,
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
            rerank=rerank,
            threshold=threshold,
            internal_recall_k=internal_recall_k,
            use_agentic=use_agentic,
        )
    ).execute()


class RetrievalExecutionPlan:
    def __init__(self, request: RetrievalQuery) -> None:
        self.request = request

    async def execute(self) -> dict[str, Any]:
        request = self.request

        # TODO(intent-step): Insert Intent Understanding step here.
        # Before any retrieval runs, parse `request.query` with LLM +
        # KG overview + section tree to extract structured navigation
        # hints (document_hint, scope_hint, content_type_hint).
        # Use extracted hints to override request.data_type,
        # request.signal_paths, request.filter_mode, and narrow
        # request.exclude_document_ids. This pre-trims the search
        # space so Discovery/DocSelect/Navigation operate on a
        # focused subgraph. Only activate when hints are detected;
        # pure semantic queries skip this step.
        # See: shared/services/retrieval/intent/ (to be created)

        start_time = time.monotonic()
        _log_retrieval_start(
            query=request.query,
            user_id=request.user_id,
            namespace=request.namespace,
            top_k=request.top_k,
            data_type=request.data_type,
            exclude_document_ids=request.exclude_document_ids,
            exclude_sections=request.exclude_sections,
        )

        if not request.query:
            logger.info("  ⛔ Empty query filtered, skipping retrieval pipeline")
            return {
                "namespace": request.namespace,
                "query": request.query,
                "router_used": "empty_query_filtered",
                "evidence_text": "",
                "answer_text": "",
                "referenced_chunks": [],
                "results": [],
            }

        allowed_chunk_types = request.resolve_allowed_chunk_types()
        effective_recall_k = request.resolve_effective_recall_k()
        logger.info(
            f"  allowed_chunk_types={allowed_chunk_types}  "
            f"effective_recall_k={effective_recall_k}  "
            f"signal_paths={request.signal_paths}  filter_mode={request.filter_mode}  "
            f"rerank={request.rerank}  threshold={request.threshold}"
        )

        cache_extra = request.build_cache_extra()
        cache_version, cached_response = await _read_cached_response(
            user_id=request.user_id,
            namespace=request.namespace,
            query=request.query,
            top_k=request.top_k,
            exclude_document_ids=request.exclude_document_ids,
            exclude_sections=request.exclude_sections,
            cache_extra=cache_extra,
        )
        if cached_response is not None:
            return cached_response

        logger.debug(f"  📦 Cache miss (version={cache_version}), running full pipeline")

        outcome = await run_retrieval_route(request.build_route_context())

        if cache_version is not None:
            await _write_cached_response(
                user_id=request.user_id,
                namespace=request.namespace,
                version=cache_version,
                query=request.query,
                top_k=request.top_k,
                exclude_document_ids=request.exclude_document_ids,
                exclude_sections=request.exclude_sections,
                response=outcome.response,
                cache_extra=cache_extra,
            )

        _schedule_hit_stats_update(
            user_id=request.user_id,
            namespace=request.namespace,
            results=outcome.hit_stats_results,
        )
        _log_retrieval_complete(
            outcome=outcome.response,
            label=outcome.completion_label,
            count=outcome.completion_count,
            detail=outcome.completion_detail,
            elapsed_ms=round((time.monotonic() - start_time) * 1000),
        )
        return await project_public_retrieval_response(outcome.response)


async def _read_cached_response(
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    cache_extra: dict[str, Any],
) -> tuple[int | None, dict[str, Any] | None]:
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
            logger.info(f"retrieval: cache_hit=True version={cache_version}")
            _schedule_hit_stats_update(
                user_id=user_id,
                namespace=namespace,
                results=cached.get("results", []),
            )
            return cache_version, await project_public_retrieval_response(cached)
    except Exception as exc:
        logger.warning(f"Failed to read retrieval cache (ignored): {exc}")
    return cache_version, None


async def _write_cached_response(
    *,
    user_id: str,
    namespace: str,
    version: int,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    response: dict[str, Any],
    cache_extra: dict[str, Any],
) -> None:
    try:
        await set_cached_retrieval_query_result(
            user_id=user_id,
            namespace=namespace,
            version=version,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            response=response,
            **cache_extra,
        )
    except Exception as exc:
        logger.warning(f"Failed to write retrieval cache (ignored): {exc}")


def _schedule_hit_stats_update(
    *,
    user_id: str,
    namespace: str,
    results: list[dict[str, Any]],
) -> None:
    try:
        schedule_retrieval_hit_stats_update(
            user_id=user_id,
            namespace=namespace,
            results=results,
        )
    except Exception as exc:
        logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {exc}")


def _log_retrieval_start(
    *,
    query: str,
    user_id: str,
    namespace: str,
    top_k: int,
    data_type: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> None:
    logger.info("\n" + "█" * 70)
    logger.info("  🚀 RETRIEVAL PIPELINE START")
    logger.info(f'  query="{query}"')
    logger.info(
        f"  user={user_id}  ns={namespace}  top_k={top_k}  data_type={data_type}"
    )
    logger.info(
        f"  exclude_docs={exclude_document_ids}  "
        f"exclude_secs={len(exclude_sections)}"
    )
    logger.info("█" * 70)


def _log_retrieval_complete(
    *,
    outcome: dict[str, Any],
    label: str,
    count: int,
    detail: str,
    elapsed_ms: int,
) -> None:
    logger.info(f'\n{"█" * 70}')
    logger.info(f"  ✅ {label} COMPLETE: {count} {detail} | {elapsed_ms}ms")
    results = outcome.get("results", [])
    if isinstance(results, list):
        for index, result in enumerate(results[:10]):
            source = result.get("source", {})
            logger.info(
                f"    [{index + 1}] type={result.get('chunk_type', '?')}  "
                f"score={result.get('score', 0):.4f}"
                f"  path={source.get('section_path', '')}"
                f"  file={source.get('source_file_name', '')}"
            )
        if len(results) > 10:
            logger.info(f"    ... and {len(results) - 10} more")
    logger.info(f'{"█" * 70}')
