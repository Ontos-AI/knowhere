"""
Canonical retrieval publication service.

This module owns the retrieval-specific publication work that happens during
job finalization. The job lifecycle service should orchestrate transaction
boundaries and call this service, not define retrieval state construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.graph.service import DocumentGraphService, GraphScope
from shared.services.retrieval.publication_content import (
    deduplicate_chunks_by_source_path,
    replace_document_revision_content,
)
from shared.services.retrieval.publication_models import (
    DocumentPublicationScope,
    ExistingDocumentScope,
    PublishedDocumentState,
)
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
    lock_namespace_generation,
)
from shared.services.retrieval.serving_manifest import (
    rebuild_namespace_serving_statistics,
)


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RetrievalPublicationService:
    # ── Public API ──────────────────────────────────────────────────────

    def get_existing_document_scope(
        self,
        db: Session,
        *,
        job_id: str,
    ) -> ExistingDocumentScope | None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            return None

        metadata = job.job_metadata or {}
        document_id = metadata.get("document_id")
        if not document_id:
            return None

        document = db.execute(
            select(Document).where(Document.document_id == document_id)
        ).scalar_one_or_none()
        if not document:
            return None

        return ExistingDocumentScope(
            document_id=document.document_id,
            namespace=document.namespace,
        )

    def publish_document_state(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str] | None = None,
    ) -> PublishedDocumentState | None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            logger.warning(f"Job not found for document publication: {job_id}")
            return None

        return self._publish_document_state_for_job(
            db,
            job=job,
            job_result_id=job_result_id,
            chunks=chunks,
            section_summaries=section_summaries,
        )

    def _publish_document_state_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str] | None = None,
    ) -> PublishedDocumentState | None:

        job_metadata = job.job_metadata or {}
        namespace = normalize_retrieval_namespace(job_metadata.get("namespace"))
        document_id = job_metadata.get("document_id")
        parse_track = str(job_metadata.get("parse_track") or "chunk")
        source_file_name = job_metadata.get("source_file_name") or job_metadata.get(
            "file_name"
        )
        document_metadata = JobMetadataHelper.get_document_metadata(job_metadata)

        deduped_chunks = deduplicate_chunks_by_source_path(chunks)

        # If ALL chunks are duplicates → skip document creation entirely
        if not deduped_chunks:
            logger.warning(
                f"⏭️  All chunks are duplicates of existing documents. "
                f"Skipping document creation for job_id={job.job_id}."
            )
            return PublishedDocumentState(
                user_id=str(job.user_id),
                namespace=namespace,
                document_id=None,
                skipped_all_duplicate=True,
            )

        existing_namespace = None
        if document_id:
            existing_namespace = db.execute(
                select(Document.namespace).where(
                    Document.document_id == str(document_id)
                )
            ).scalar_one_or_none()
        namespaces_to_lock = {namespace}
        if existing_namespace:
            namespaces_to_lock.add(str(existing_namespace))
        for namespace_to_lock in sorted(namespaces_to_lock):
            lock_namespace_generation(
                db,
                user_id=str(job.user_id),
                namespace=namespace_to_lock,
            )

        document = self._upsert_document_revision(
            db,
            job=job,
            job_result_id=job_result_id,
            document_id=str(document_id) if document_id else None,
            namespace=namespace,
            parse_track=parse_track,
            source_file_name=str(source_file_name) if source_file_name else None,
            document_metadata=document_metadata,
        )
        if document is None:
            return None

        self._bind_job_result_document(
            db,
            job_result_id=job_result_id,
            document_id=document.document_id,
        )
        namespace = normalize_retrieval_namespace(namespace or document.namespace)
        scope = DocumentPublicationScope(
            user_id=str(job.user_id),
            namespace=namespace,
            document_id=document.document_id,
            job_result_id=job_result_id,
            source_file_name=str(source_file_name) if source_file_name else None,
        )
        replace_document_revision_content(
            db,
            scope=scope,
            chunks=deduped_chunks,
            section_summaries=section_summaries,
        )

        db.flush()
        rebuild_namespace_serving_statistics(
            db,
            user_id=scope.user_id,
            namespace=scope.namespace,
        )
        if existing_namespace and str(existing_namespace) != scope.namespace:
            rebuild_namespace_serving_statistics(
                db,
                user_id=scope.user_id,
                namespace=str(existing_namespace),
            )
        advance_namespace_generation(
            db,
            user_id=scope.user_id,
            namespace=scope.namespace,
        )
        return PublishedDocumentState(
            user_id=str(job.user_id),
            namespace=namespace,
            document_id=document.document_id,
        )

    def _upsert_document_revision(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        document_id: str | None,
        namespace: str,
        parse_track: str,
        source_file_name: str | None,
        document_metadata: dict[str, Any],
    ) -> Document | None:
        document = None
        if document_id:
            document = db.execute(
                select(Document)
                .where(
                    Document.document_id == document_id,
                    Document.user_id == str(job.user_id),
                )
                .with_for_update()
            ).scalar_one_or_none()

        if document is None:
            document = Document(
                document_id=document_id or f"doc_{uuid4().hex[:12]}",
                user_id=str(job.user_id),
                namespace=namespace,
                status="active",
                current_job_result_id=job_result_id,
                source_file_name=source_file_name,
                document_metadata=document_metadata,
                parse_track=parse_track,
            )
            db.add(document)
        else:
            if self._is_stale_document_completion(
                db,
                document=document,
                job=job,
            ):
                logger.warning(
                    "Skipping stale document publication: "
                    f"job_id={job.job_id}, document_id={document.document_id}"
                )
                return None
            document.status = "active"
            document.namespace = namespace
            document.archived_at = None
            document.current_job_result_id = job_result_id
            document.source_file_name = source_file_name or document.source_file_name
            if document_metadata:
                document.document_metadata = document_metadata
            document.parse_track = parse_track or document.parse_track
            document.updated_at = utc_now_naive()

        db.flush()
        return document

    def _bind_job_result_document(
        self,
        db: Session,
        *,
        job_result_id: str,
        document_id: str,
    ) -> None:
        result = db.execute(select(JobResult).where(JobResult.id == job_result_id))
        job_result = result.scalar_one_or_none()
        if job_result:
            job_result.document_id = document_id

    def publish_document_graph(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        top_summary: str | None = None,
    ) -> None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            raise RuntimeError(f"Job not found for graph publication: {job_id}")

        self._publish_document_graph_for_job(
            db,
            job=job,
            job_result_id=job_result_id,
            top_summary=top_summary,
        )

    def _publish_document_graph_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        top_summary: str | None = None,
    ) -> None:

        metadata = job.job_metadata or {}
        namespace = normalize_retrieval_namespace(metadata.get("namespace"))
        document_id = metadata.get("document_id")
        if not document_id:
            document = db.execute(
                select(Document).where(Document.current_job_result_id == job_result_id)
            ).scalar_one_or_none()
            document_id = document.document_id if document else None
        if not document_id:
            raise RuntimeError(
                f"Document not found for graph publication: job_id={job.job_id}"
            )

        DocumentGraphService().publish_document_graph(
            db,
            user_id=str(job.user_id),
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
            top_summary=top_summary,
        )

    def remove_document_graph(
        self,
        db: Session,
        *,
        user_id: str,
        namespace: str,
        document_id: str,
    ) -> None:
        DocumentGraphService().remove_document_graph(
            db,
            scope=GraphScope(user_id=user_id, namespace=namespace),
            document_id=document_id,
        )

    def _is_stale_document_completion(
        self,
        db: Session,
        *,
        document: Document,
        job: Job,
    ) -> bool:
        current_job_result_id = getattr(document, "current_job_result_id", None)
        if not current_job_result_id:
            return False

        current_job_result = db.execute(
            select(JobResult).where(JobResult.id == current_job_result_id)
        ).scalar_one_or_none()
        current_job_id = getattr(current_job_result, "job_id", None)
        if current_job_result is None or not current_job_id:
            return False

        current_job = db.execute(
            select(Job).where(Job.job_id == current_job_id)
        ).scalar_one_or_none()
        if current_job is None:
            return False

        current_created_at = getattr(current_job, "created_at", None)
        candidate_created_at = getattr(job, "created_at", None)
        if current_created_at is None or candidate_created_at is None:
            return False

        return current_created_at > candidate_created_at
