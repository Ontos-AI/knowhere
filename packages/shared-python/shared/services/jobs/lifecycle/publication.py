from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shared.models.database.document import DocumentSection
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory
from shared.services.retrieval.publication_service import RetrievalPublicationService


@dataclass(frozen=True)
class JobPublicationOutcome:
    published_document_state: dict[str, str] | None
    cache_invalidation: dict[str, Any] | None


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
    ) -> JobPublicationOutcome:
        previous_document_scope = self._retrieval_publication.get_existing_document_scope(
            db,
            job_id=job_id,
        )
        published_document_state = self._retrieval_publication.publish_document_state(
            db,
            job_id=job_id,
            job_result_id=job_result_id,
            chunks=chunks,
        )
        if _should_publish_document_graph(published_document_state):
            assert published_document_state is not None
            if section_summaries:
                self._backfill_section_summaries(
                    db,
                    document_id=published_document_state.get("document_id", ""),
                    job_result_id=job_result_id,
                    section_summaries=section_summaries,
                )
            self._retrieval_publication.publish_document_graph(
                db,
                job_id=job_id,
                job_result_id=job_result_id,
            )

        cache_invalidation = self._build_cache_invalidation(
            db,
            job_id=job_id,
            published_document_state=published_document_state,
            previous_document_scope=previous_document_scope,
        )
        return JobPublicationOutcome(
            published_document_state=published_document_state,
            cache_invalidation=cache_invalidation,
        )

    def invalidate_cache_after_commit(
        self,
        cache_invalidation: dict[str, Any] | None,
    ) -> None:
        if not cache_invalidation:
            return

        try:
            redis_service = SyncRedisServiceFactory.get_service()
            user_id = cache_invalidation["user_id"]
            seen: set[str] = set()
            for raw_namespace in cache_invalidation["namespaces"]:
                namespace = normalize_retrieval_namespace(str(raw_namespace))
                if not namespace or namespace in seen:
                    continue
                seen.add(namespace)
                redis_service.incr(f"retrieval:version:{user_id}:{namespace}")
        except Exception as exc:
            logger.warning(
                "Failed to invalidate retrieval cache after publication "
                f"(ignored): job_id={cache_invalidation.get('job_id')}, error={exc}"
            )

    def _backfill_section_summaries(
        self,
        db: Session,
        *,
        document_id: str,
        job_result_id: str,
        section_summaries: dict[str, str],
    ) -> None:
        if not document_id or not section_summaries:
            return

        try:
            for path, summary in section_summaries.items():
                if not path or not summary:
                    continue
                db.execute(
                    update(DocumentSection)
                    .where(DocumentSection.document_id == document_id)
                    .where(DocumentSection.job_result_id == job_result_id)
                    .where(DocumentSection.section_path == path)
                    .values(summary=summary)
                )
            db.flush()
            logger.debug(
                f"Backfilled section summaries: document_id={document_id}, "
                f"count={len(section_summaries)}"
            )
        except Exception as exc:
            logger.warning(f"Section summary backfill failed (non-fatal): {exc}")

    def _build_cache_invalidation(
        self,
        db: Session,
        *,
        job_id: str,
        published_document_state: dict[str, str] | None,
        previous_document_scope: dict[str, str] | None,
    ) -> dict[str, Any] | None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            return None

        metadata = job.job_metadata or {}
        namespaces = [
            JobMetadataHelper.get_namespace(metadata, "default") or "default",
        ]
        if previous_document_scope and previous_document_scope.get("namespace"):
            namespaces.append(previous_document_scope["namespace"])
        if published_document_state and published_document_state.get("namespace"):
            namespaces.append(published_document_state["namespace"])

        return {"user_id": str(job.user_id), "namespaces": namespaces, "job_id": job_id}


def _should_publish_document_graph(
    published_document_state: dict[str, str] | None,
) -> bool:
    return published_document_state is not None and not published_document_state.get(
        "skipped_all_duplicate"
    )
