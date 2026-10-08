"""
Canonical retrieval publication service.

This module owns the retrieval-specific publication work that happens during
job finalization. The job lifecycle service should orchestrate transaction
boundaries and call this service, not define retrieval state construction.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_job_scope import validate_job_corpus

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.graph.service import DocumentGraphService, GraphScope
from shared.services.retrieval.namespace_map_snapshot import (
    patch_namespace_map_snapshot,
    remove_document_from_namespace_map_snapshot,
)
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
from shared.services.retrieval.publication_trace_stage import trace_publication_stage

if TYPE_CHECKING:
    from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RetrievalPublicationService:
    # ── Public API ──────────────────────────────────────────────────────

    def get_existing_document_scope(
        self,
        db: Session,
        *,
        job_id: str,
        trace: PublicationTrace | None = None,
    ) -> ExistingDocumentScope | None:
        with trace_publication_stage(trace, "document_resolution"):
            return self._get_existing_document_scope(db, job_id=job_id)

    def _get_existing_document_scope(
        self, db: Session, *, job_id: str
    ) -> ExistingDocumentScope | None:
        job: Job | None = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if job is None:
            return None
        corpusStorage: CorpusStorage = validate_job_corpus(db, job=job)
        metadata = job.job_metadata or {}
        document = db.execute(select(corpusStorage.Document).where(corpusStorage.Document.document_id == metadata.get("document_id"), corpusStorage.Document.user_id == corpusStorage.resolve_owner(str(job.user_id)))).scalar_one_or_none()
        if document is None:
            return None
        document_id, namespace = document.document_id, document.namespace

        return ExistingDocumentScope(
            document_id=str(document_id),
            namespace=str(namespace),
        )

    def publish_document_state(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str] | None = None,
        update_namespace_snapshot: bool = True,
        trace: PublicationTrace | None = None,
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
            update_namespace_snapshot=update_namespace_snapshot,
            trace=trace,
        )

    def _publish_document_state_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str] | None = None,
        update_namespace_snapshot: bool = True,
        trace: PublicationTrace | None = None,
    ) -> PublishedDocumentState | None:

        corpusStorage: CorpusStorage = validate_job_corpus(db, job=job)
        job_metadata = job.job_metadata or {}
        namespace = normalize_retrieval_namespace(job_metadata.get("namespace"))
        document_id = job_metadata.get("document_id")
        parse_track = str(job_metadata.get("parse_track") or "chunk")
        source_file_name = job_metadata.get("source_file_name") or job_metadata.get(
            "file_name"
        )
        document_metadata = JobMetadataHelper.get_document_metadata(job_metadata)

        # A job result is one immutable publication revision. Once it has
        # been bound to a document, a replay of the same completion must not
        # create another document or rewrite serving state. Locking the row
        # also serializes concurrent replays of the same revision.
        existing_document_id = db.execute(
            select(JobResult.demo_document_id if corpusStorage.is_demo else JobResult.document_id)
            .where(JobResult.id == job_result_id)
            .with_for_update()
        ).scalar_one_or_none()
        if existing_document_id:
            logger.info(
                "Skipping duplicate document publication: "
                f"job_id={job.job_id}, job_result_id={job_result_id}, "
                f"document_id={existing_document_id}"
            )
            return PublishedDocumentState(
                user_id=str(job.user_id),
                namespace=namespace,
                document_id=None,
                skipped_all_duplicate=True,
            )

        # Keep bulk GIN updates inside the publication transaction while
        # allowing background cleanup between publications.
        db.execute(text("SET LOCAL gin_pending_list_limit = '64MB'"))

        with trace_publication_stage(trace, "chunks_prepare"):
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

        document, existing_namespace = self._upsert_document_revision(
            db,
            job=job,
            job_result_id=job_result_id,
            document_id=str(document_id) if document_id else None,
            namespace=namespace,
            parse_track=parse_track,
            source_file_name=str(source_file_name) if source_file_name else None,
            document_metadata=document_metadata,
            trace=trace,
        )
        if document is None:
            return None

        with trace_publication_stage(trace, "result_binding"):
            self._bind_job_result_document(
                db,
                job_result_id=job_result_id,
                document_id=document.document_id,
                is_demo=corpusStorage.is_demo,
            )
        namespace = normalize_retrieval_namespace(namespace or document.namespace)
        scope = DocumentPublicationScope(
            user_id=corpusStorage.resolve_owner(str(job.user_id)),
            namespace=namespace,
            document_id=document.document_id,
            job_result_id=job_result_id,
            source_file_name=str(source_file_name) if source_file_name else None,
        )
        if trace is not None:
            trace.record_count("chunks", len(deduped_chunks))
        manifest_payload = replace_document_revision_content(
            db,
            scope=scope,
            chunks=deduped_chunks,
            section_summaries=section_summaries,
            trace=trace,
        )

        if corpusStorage.is_demo:
            from shared.services.retrieval.demo_examples import bind_demo_examples
            bind_demo_examples(db, document_id=scope.document_id, job_result_id=job_result_id)

        if update_namespace_snapshot:
            self.update_namespace_snapshot(
                db,
                scope=scope,
                manifest_payload=manifest_payload,
                previous_namespace=(
                    str(existing_namespace)
                    if existing_namespace and str(existing_namespace) != scope.namespace
                    else None
                ),
                trace=trace,
            )
        return PublishedDocumentState(
            user_id=str(job.user_id),
            namespace=namespace,
            document_id=document.document_id,
            manifest_payload=manifest_payload,
            previous_namespace=(
                str(existing_namespace)
                if existing_namespace and str(existing_namespace) != scope.namespace
                else None
            ),
        )

    def update_namespace_snapshot(
        self,
        db: Session,
        *,
        scope: DocumentPublicationScope,
        manifest_payload: dict[str, Any],
        previous_namespace: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> None:
        """Patch one namespace snapshot while holding only its short lock."""
        namespaces = [scope.namespace]
        if previous_namespace:
            namespaces.insert(0, previous_namespace)
        for namespace in namespaces:
            with trace_publication_stage(trace, "namespace_generation_lock_wait"):
                generation = lock_namespace_generation(
                    db,
                    user_id=scope.user_id,
                    namespace=namespace,
                )
            if namespace == scope.namespace:
                patch_namespace_map_snapshot(
                    db,
                    scope=scope,
                    manifest_payload=manifest_payload,
                    current_generation=generation,
                    trace=trace,
                )
            else:
                remove_document_from_namespace_map_snapshot(
                    db,
                    user_id=scope.user_id,
                    namespace=namespace,
                    document_id=scope.document_id,
                    current_generation=generation,
                    trace=trace,
                )
            with trace_publication_stage(trace, "namespace_generation_lock_wait"):
                advance_namespace_generation(
                    db,
                    user_id=scope.user_id,
                    namespace=namespace,
                    locked_generation=generation,
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
        trace: PublicationTrace | None = None,
    ) -> tuple[Document | None, str | None]:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        document = None
        previous_namespace: str | None = None
        if document_id:
            with trace_publication_stage(trace, "existing_document_lock_wait"):
                document = db.execute(
                    select(corpusStorage.Document)
                    .where(
                        corpusStorage.Document.document_id == document_id,
                        corpusStorage.Document.user_id == corpusStorage.resolve_owner(str(job.user_id)),
                    )
                    .with_for_update()
                ).scalar_one_or_none()
            if document is not None:
                previous_namespace = document.namespace

        with trace_publication_stage(trace, "revision_update"):
            if document is None:
                document = corpusStorage.Document(
                    document_id=document_id or f"doc_{uuid4().hex[:12]}",
                    user_id=corpusStorage.resolve_owner(str(job.user_id)),
                    namespace=namespace,
                    status="active",
                    current_job_result_id=job_result_id,
                    source_file_name=source_file_name,
                    document_metadata=document_metadata,
                    parse_track=parse_track,
                )
                db.add(document)
            else:
                if corpusStorage.is_demo and document.status == "archived":
                    return None, previous_namespace
                if self._is_stale_document_completion(
                    db,
                    document=document,
                    job=job,
                ):
                    logger.warning(
                        "Skipping stale document publication: "
                        f"job_id={job.job_id}, document_id={document.document_id}"
                    )
                    return None, previous_namespace
                document.status = "active"
                document.namespace = namespace
                document.archived_at = None
                document.current_job_result_id = job_result_id
                document.source_file_name = (
                    source_file_name or document.source_file_name
                )
                if document_metadata:
                    document.document_metadata = document_metadata
                document.parse_track = parse_track or document.parse_track
                document.updated_at = utc_now_naive()

            db.flush()
            return document, previous_namespace

    def _bind_job_result_document(
        self,
        db: Session,
        *,
        job_result_id: str,
        document_id: str,
        is_demo: bool = False,
    ) -> None:
        db.execute(
            update(JobResult)
            .where(JobResult.id == job_result_id)
            .values(**({"demo_document_id": document_id} if is_demo else {"document_id": document_id}))
        )

    def publish_document_graph(
        self,
        db: Session,
        *,
        job_id: str,
        job_result_id: str,
        top_summary: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> None:
        job = db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()
        if not job:
            raise RuntimeError(f"Job not found for graph publication: {job_id}")

        self._publish_document_graph_for_job(
            db,
            job=job,
            job_result_id=job_result_id,
            top_summary=top_summary,
            trace=trace,
        )

    def _publish_document_graph_for_job(
        self,
        db: Session,
        *,
        job: Job,
        job_result_id: str,
        top_summary: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> None:

        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace((job.job_metadata or {}).get('namespace'))
        metadata = job.job_metadata or {}
        namespace = normalize_retrieval_namespace(metadata.get("namespace"))
        document_id = metadata.get("document_id")
        if not document_id:
            document = db.execute(
                select(corpusStorage.Document).where(corpusStorage.Document.current_job_result_id == job_result_id)
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
            trace=trace,
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
