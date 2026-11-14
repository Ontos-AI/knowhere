from __future__ import annotations

import time
from dataclasses import replace
from hashlib import sha256
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ConflictException, UnavailableException
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_query_scope import validate_demo_query_scope
from shared.services.retrieval.document_scope import DocumentScope

from shared.models.schemas.llm_config import LLMConfig
from shared.services.ai.llm_overrides import (
    reset_llm_overrides_async,
    set_llm_overrides_async,
)
from shared.services.retrieval.agent_explore.cursor_retrieval_exception import (
    CursorRetrievalException,
)
from shared.services.retrieval.cache_service import (
    get_cached_retrieval_query_result,
    set_cached_retrieval_query_result,
)
from shared.services.retrieval.execution.routes import run_retrieval_route
from shared.services.retrieval.execution.revision_pins import (
    RetrievalRevisionPins,
    capture_revision_pins,
    is_revision_generation_stable,
)
from shared.services.retrieval.stats.recorder import (
    schedule_retrieval_hit_stats_update,
)
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.execution.query_request import RetrievalQuery
from shared.services.retrieval.execution.native_failure import NativeFailureState
from shared.services.retrieval.execution.postgresql_snapshot import PostgreSQLServingGenerationChanged
from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.reader import PublishedLexicalRead
from shared.services.retrieval.lexical.stage_observer import StageObserver
from shared.services.retrieval.manifest_cache import discard_cached_manifest_payloads
from shared.services.retrieval.required_query_embedding import (
    RequiredQueryEmbeddingException,
)


class _NativeServingGenerationChanged(RuntimeError):
    """A progressive publication crossed a native retrieval attempt."""


async def run_retrieval_query(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    include_document_ids: list[str] | None = None,
    chunk_types: set[str] | None = None,
    signal_paths: list[str] | None = None,
    filter_mode: str = "delete",
    rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
    use_agentic: bool | None = None,
    agent_explore_model: str | None = None,
    conversation_id: str | None = None,
    llm_config: LLMConfig | None = None,
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
            include_document_ids=include_document_ids,
            chunk_types=chunk_types,
            signal_paths=signal_paths,
            filter_mode=filter_mode,
            rerank=rerank,
            threshold=threshold,
            internal_recall_k=internal_recall_k,
            use_agentic=use_agentic,
            agent_explore_model=agent_explore_model,
            conversation_id=conversation_id,
            llm_config=llm_config,
        )
    ).execute()


class RetrievalExecutionPlan:
    def __init__(self, request: RetrievalQuery) -> None:
        self.request = request
        self._postgresqlEpoch: int | None = None

    async def execute(self) -> dict[str, Any]:
        request = self.request
        override_token = set_llm_overrides_async(request.llm_config)

        try:
            if settings.KNOWHERE_LEXICAL_BACKEND == "opensearch" and request.query:
                try:
                    return await self._execute_native(request)
                except _NativeServingGenerationChanged:
                    logger.warning("Progressive publication changed native retrieval generation; retrying once")
                    try:
                        return await self._execute_native(request, can_retry_classic=False)
                    except _NativeServingGenerationChanged:
                        raise self._serving_generation_conflict() from None
            if request.query:
                self._postgresqlEpoch = await self._capture_postgresql_epoch(request)
            return await self._execute_with_overrides(request)
        except LexicalIndexError as error:
            raise UnavailableException(internal_message=str(error), retry_after=1,
                user_message="Search is temporarily unavailable. Please retry.", original_exception=error) from error
        finally:
            reset_llm_overrides_async(override_token)

    @staticmethod
    async def _capture_postgresql_epoch(request: RetrievalQuery) -> int:
        bind: AsyncEngine | AsyncConnection | None = getattr(request.db, "bind", None)
        if bind is None:
            raise LexicalIndexError("API retrieval requires an asynchronous database bind")
        # Capture the operator gate before cache/shortcuts. Reuse the existing
        # async engine and finish this short transaction before any route work.
        async with AsyncSession(bind=bind) as gateDb:
            state: tuple[str, str, int] | None = (await gateDb.execute(select(
                LexicalBackendControl.backend, LexicalBackendControl.mode, LexicalBackendControl.epoch,
            ).where(LexicalBackendControl.id == 1))).tuples().one_or_none()
        if state is None:
            # Legacy PG scopes predate the optional native control row; reads
            # never initialize it. First admission establishes a new epoch/key.
            return 0
        backend, mode, epoch = state
        if backend != "postgresql" or mode != "serving":
            raise LexicalIndexError("PostgreSQL retrieval is gated or another backend is selected", isRetryable=True)
        return epoch

    async def _execute_native(
        self, request: RetrievalQuery, *, can_retry_classic: bool = True,
    ) -> dict[str, Any]:
        reader: AsyncPublishedLexicalRead | None = None
        failureState: NativeFailureState = NativeFailureState()
        try:
            bind: AsyncEngine | AsyncConnection | None = getattr(request.db, "bind", None)
            if bind is None:
                raise LexicalIndexError("API retrieval requires an asynchronous database bind")
            reader = AsyncPublishedLexicalRead(userId=request.user_id,
                namespace=request.namespace, bind=bind, isClassic=request.use_agentic is False,
                preferContentCoverage=request.use_agentic is False and request.include_document_ids is not None)
            admittedManifest: bool = (
                reader.isClassic
                and reader.scope.corpusKind == "private" and not request.signal_paths
                and await reader.startManifestAsync(documentScope=request.build_route_context().document_scope,
                    chunkTypes=request.resolve_allowed_chunk_types(), topK=request.top_k)
            )
            if not admittedManifest:
                await reader.startAsync()
            with reader.bind(), failureState.bind():
                async with AsyncSession(bind=bind, expire_on_commit=False) as ownedDb:
                    ownedDb.info["lexical_read_owned"] = True
                    response = await self._execute_with_overrides(
                        replace(request, db=ownedDb), can_retry_classic=can_retry_classic,
                    )
                    failureState.raiseIfFailed()
                    if reader.isClassic:
                        await reader.finishAsync(ownedDb)
                        await ownedDb.commit()
                    else:
                        await reader.finishAsync()
                    return response
        except Exception as error:
            if isinstance(error, LexicalIndexError):
                failureState.record(error)
            nativeError: LexicalIndexError | None = failureState.failure
            if nativeError is None:
                raise
            logger.warning("retrieval native failure: stage=request error_type={} retryable={}",
                type(nativeError).__name__, nativeError.isRetryable)
            raise UnavailableException(internal_message=str(nativeError), retry_after=1,
                user_message="Search is temporarily unavailable. Please retry.", original_exception=nativeError) from nativeError
        finally:
            try:
                if reader is not None and not reader.isFinished:
                    await reader.closeAsync()
            except Exception as error:
                logger.warning("Lexical read close failed: error_type={}", type(error).__name__)

    @staticmethod
    def _serving_generation_conflict() -> ConflictException:
        return ConflictException(
            user_message="Document search changed during retrieval. Please retry.",
            reason="ABORTED",
            resource="Namespace",
        )

    @staticmethod
    async def _is_serving_read_stable(
        request: RetrievalQuery, pins: RetrievalRevisionPins,
        native_read: PublishedLexicalRead | None, scope: DocumentScope,
    ) -> bool:
        if native_read is not None:
            return await native_read.isProgressivePublicationStable(request.db, scope)
        return await is_revision_generation_stable(
            request.db, user_id=request.user_id, namespace=request.namespace, pins=pins, strict=True,
        )

    async def _execute_with_overrides(
        self, request: RetrievalQuery, *, can_retry_classic: bool = True,
    ) -> dict[str, Any]:
        # TODO(intent-step): Insert Intent Understanding step here.
        # Before any retrieval runs, parse `request.query` with LLM +
        # KG overview + section tree to extract structured navigation
        # hints (document_hint, scope_hint, content_type_hint).
        # Use extracted hints to override request.chunk_types,
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
            chunk_types=request.chunk_types,
            exclude_document_ids=request.exclude_document_ids,
            exclude_sections=request.exclude_sections,
        )

        if not request.query:
            logger.info("  ⛔ Empty query filtered, skipping retrieval pipeline")
            return {
                "namespace": request.namespace,
                "query": request.query,
                "router_used": "empty_query_filtered",
                "failure_reason": "empty query — retrieval was not run",
                "evidence": [],
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

        native_read: PublishedLexicalRead | None = PublishedLexicalRead.current()
        is_demo: bool = CorpusStorage.resolve_namespace(request.namespace).is_demo
        captured_pins: RetrievalRevisionPins | None = (
            native_read.pins if native_read is not None and native_read.manifestId is None else None
        )
        cache_extra = request.build_cache_extra()
        isPostgresqlClassic: bool = self._postgresqlEpoch is not None and request.use_agentic is not True
        if (is_demo or isPostgresqlClassic) and captured_pins is None:
            ownedCapture: bool = isPostgresqlClassic and not request.db.in_transaction()
            captured_pins = await capture_revision_pins(
                request.db, user_id=request.user_id, namespace=request.namespace
            )
            if ownedCapture:
                await request.db.rollback()
        if is_demo:
            assert captured_pins is not None
            await validate_demo_query_scope(
                request.db,
                include_document_ids=request.include_document_ids,
                pins=captured_pins,
            )
        if captured_pins is not None:
            cache_extra["corpus_generation"] = captured_pins.generation
            cache_extra["revision_digest"] = sha256(
                repr(sorted(captured_pins.items())).encode()
            ).hexdigest()
        if native_read is not None:
            cache_extra["lexical_backend"] = "opensearch"
            cache_extra["backend_epoch"] = native_read.epoch
            cache_extra["published_manifest_id"] = native_read.manifestId
            if (
                isinstance(native_read, AsyncPublishedLexicalRead)
                and native_read.preferContentCoverage
            ):
                cache_extra["lexical_ranking_profile"] = "document-content-coverage-v1"
        elif self._postgresqlEpoch is not None:
            cache_extra["lexical_backend"] = "postgresql"
            cache_extra["backend_epoch"] = self._postgresqlEpoch

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
            StageObserver.record("bypass", "cache")
            return cached_response

        logger.debug(
            f"  📦 Cache miss (version={cache_version}), running full pipeline"
        )

        route_context = request.build_route_context()
        if native_read is not None and native_read.manifestId is not None:
            outcome = await run_retrieval_route(route_context)
        else:
            for attempt in range(2):
                revision_pins: RetrievalRevisionPins | None = (
                    captured_pins if attempt == 0 else None
                )
                if revision_pins is None:
                    ownedCapture = isPostgresqlClassic and not request.db.in_transaction()
                    revision_pins = await capture_revision_pins(
                        request.db,
                        user_id=request.user_id,
                        namespace=request.namespace,
                    )
                    if ownedCapture:
                        await request.db.rollback()
                if is_demo and attempt > 0:
                    await validate_demo_query_scope(
                        request.db,
                        include_document_ids=request.include_document_ids,
                        pins=revision_pins,
                    )
                if (is_demo or isPostgresqlClassic) and attempt > 0:
                    cache_extra["corpus_generation"] = revision_pins.generation
                    cache_extra["revision_digest"] = sha256(
                        repr(sorted(revision_pins.items())).encode()
                    ).hexdigest()
                try:
                    with CorpusRevisionContext.bind(revision_pins):
                        outcome = await run_retrieval_route(
                            replace(
                                route_context,
                                revision_pins=revision_pins,
                                document_scope=route_context.document_scope.narrow(
                                    list(revision_pins)
                                ),
                            )
                        )
                except (
                    CursorRetrievalException,
                    RequiredQueryEmbeddingException,
                    LexicalIndexError,
                ):
                    raise
                except PostgreSQLServingGenerationChanged:
                    pass
                except Exception:
                    if await self._is_serving_read_stable(
                        request, revision_pins, native_read, route_context.document_scope,
                    ):
                        raise
                else:
                    is_classic: bool = outcome.response.get("router_used") in {
                        "classic_topk", "small_corpus_all",
                    }
                    if is_classic and (
                        outcome.completion_count or not outcome.has_unhydrated_hits
                        or attempt > 0 or not can_retry_classic
                    ):
                        break
                    if await self._is_serving_read_stable(
                        request, revision_pins, native_read, route_context.document_scope,
                    ):
                        break
                if native_read is not None:
                    # Retry with a fresh native read, cache key, and SQL session.
                    raise _NativeServingGenerationChanged
                if attempt == 1:
                    raise self._serving_generation_conflict()
                # Agent tools intentionally use short-lived sessions across LLM waits.
                # Validate the whole episode instead of retaining a long DB snapshot.
                await request.db.rollback()
                discard_cached_manifest_payloads(request.db)
                logger.warning(
                    "Retrieval serving generation changed; retrying once user={} namespace={}",
                    request.user_id,
                    request.namespace,
                )
        NativeFailureState.raiseCurrent()
        if outcome.response.get("router_used") == "small_corpus_all":
            StageObserver.record("bypass", "small_corpus")

        if cache_version is not None and not outcome.has_unhydrated_hits:
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
    chunk_types: set[str] | None,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> None:
    logger.info("\n" + "█" * 70)
    logger.info("  🚀 RETRIEVAL PIPELINE START")
    logger.info(f'  query="{query}"')
    logger.info(
        f"  user={user_id}  ns={namespace}  top_k={top_k}  chunk_types={chunk_types}"
    )
    logger.info(
        f"  exclude_docs={exclude_document_ids}  exclude_secs={len(exclude_sections)}"
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
    logger.info(f"\n{'█' * 70}")
    logger.info(f"  ✅ {label} COMPLETE: {count} {detail} | {elapsed_ms}ms")
    results = outcome.get("results", [])
    if isinstance(results, list):
        for index, result in enumerate(results[:10]):
            logger.info(
                f"    [{index + 1}] type={result.get('chunk_type', '?')}  "
                f"score={result.get('score') or 0.0:.4f}"
                f"  path={result.get('section_path') or ''}"
                f"  file={result.get('source_file_name') or ''}"
            )
        if len(results) > 10:
            logger.info(f"    ... and {len(results) - 10} more")
    logger.info(f"{'█' * 70}")
