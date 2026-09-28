from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory
from shared.services.redis.publication_semaphore import SyncRedisPublicationSemaphore
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.core.config import settings
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.publication_models import (
    ExistingDocumentScope,
    PublishedDocumentState,
)


@dataclass(frozen=True)
class RetrievalCacheInvalidation:
    user_id: str
    namespaces: tuple[str, ...]
    job_id: str


@dataclass(frozen=True)
class JobPublicationOutcome:
    published_document_state: PublishedDocumentState | None
    cache_invalidation: RetrievalCacheInvalidation | None


class SyncJobPublicationFinalizer:
    """Publish terminal parse results and invalidate retrieval cache after commit."""

    def __init__(
        self,
        *,
        retrieval_publication: RetrievalPublicationService | None = None,
    ) -> None:
        self._retrieval_publication = (
            retrieval_publication or RetrievalPublicationService()
        )

    def publish_result(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str] | None,
        document_top_summary: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> JobPublicationOutcome:
        semaphore = SyncRedisPublicationSemaphore(
            SyncRedisServiceFactory.get_service(),
            concurrency=settings.MATERIALIZATION_DB_PUBLICATION_CONCURRENCY,
            lease_seconds=settings.MATERIALIZATION_DB_PUBLICATION_LEASE_SECONDS,
            acquire_timeout_seconds=settings.MATERIALIZATION_DB_PUBLICATION_ACQUIRE_TIMEOUT_SECONDS,
        )
        job_type = db.execute(
            select(Job.job_type).where(Job.job_id == job_id)
        ).scalar_one_or_none()
        if trace is not None and job_type in ("document_ingestion", "demo_materialization"):
            trace.bind_job_type(str(job_type))
        publication_context = (
            semaphore if job_type == "demo_materialization" else nullcontext()
        )
        with publication_context:
            previous_document_scope = self._retrieval_publication.get_existing_document_scope(
                db,
                job_id=job_id,
                trace=trace,
            )
            published_document_state = self._retrieval_publication.publish_document_state(
                db,
                job_id=job_id,
                job_result_id=job_result_id,
                chunks=chunks,
                section_summaries=section_summaries,
                trace=trace,
            )
            if trace is not None and published_document_state is not None:
                if published_document_state.document_id is not None:
                    trace.bind_document_id(published_document_state.document_id)
                trace.bind_scope(
                    user_id=published_document_state.user_id,
                    namespace=published_document_state.namespace,
                )
            if _should_publish_document_graph(published_document_state):
                assert published_document_state is not None
                self._retrieval_publication.publish_document_graph(
                    db,
                    job_id=job_id,
                    job_result_id=job_result_id,
                    top_summary=document_top_summary,
                    trace=trace,
                )

        cache_invalidation = self._build_cache_invalidation(
            db,
            job_id=job_id,
            published_document_state=published_document_state,
            previous_document_scope=previous_document_scope,
            trace=trace,
        )
        return JobPublicationOutcome(
            published_document_state=published_document_state,
            cache_invalidation=cache_invalidation,
        )

    def invalidate_cache_after_commit(
        self,
        cache_invalidation: RetrievalCacheInvalidation | None,
    ) -> None:
        if not cache_invalidation:
            return

        try:
            redis_service = SyncRedisServiceFactory.get_service()
            user_id = cache_invalidation.user_id
            seen: set[str] = set()
            for raw_namespace in cache_invalidation.namespaces:
                namespace = normalize_retrieval_namespace(str(raw_namespace))
                if not namespace or namespace in seen:
                    continue
                seen.add(namespace)
                redis_service.incr(f"retrieval:version:{user_id}:{namespace}")
        except Exception as exc:
            logger.warning(
                "Failed to invalidate retrieval cache after publication "
                f"(ignored): job_id={cache_invalidation.job_id}, error={exc}"
            )

    def _build_cache_invalidation(
        self,
        db: Session,
        *,
        job_id: str,
        published_document_state: PublishedDocumentState | None,
        previous_document_scope: ExistingDocumentScope | None,
        trace: PublicationTrace | None = None,
    ) -> RetrievalCacheInvalidation | None:
        job_row = db.execute(
            select(Job.user_id, Job.job_metadata)
            .where(Job.job_id == job_id)
        ).one_or_none()
        if job_row is None:
            return None

        user_id, raw_metadata = job_row
        metadata = raw_metadata or {}
        if trace is not None:
            parse_track = JobMetadataHelper.get_parse_track(metadata)
            if parse_track in ("chunk", "page_memory"):
                trace.bind_parse_track(parse_track)
        if published_document_state is None or published_document_state.document_id is None:
            return None
        namespaces: list[str] = [
            JobMetadataHelper.get_namespace(metadata, "default") or "default",
        ]
        if previous_document_scope:
            namespaces.append(previous_document_scope.namespace)
        if published_document_state:
            namespaces.append(published_document_state.namespace)

        return RetrievalCacheInvalidation(
            user_id=str(user_id),
            namespaces=tuple(namespaces),
            job_id=job_id,
        )


def _should_publish_document_graph(
    published_document_state: PublishedDocumentState | None,
) -> bool:
    return (
        published_document_state is not None
        and not published_document_state.skipped_all_duplicate
    )
