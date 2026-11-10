"""Persist a usable parsed revision without changing published retrieval scope."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.result_writer import SyncJobResultWriter
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.jobs.lifecycle.success_finalizer import JobSuccessFinalization, JobSuccessResponse
from shared.services.retrieval.demo_examples import bind_demo_examples
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.retrieval.publication_content import deduplicate_chunks_by_source_path, replace_document_revision_content
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.serving_generation import lock_namespace_generation
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.jobs.parsing import ParsingOwnership


class ParsedRevisionFinalizer:
    def finalize(
        self, db: Session, *, jobId: str, resultKey: str, checksum: str,
        resultSize: int, chunks: list[dict[str, object]], storedCount: int,
        deliveryMode: str, sectionSummaries: dict[str, str] | None,
        documentTopSummary: str | None,
        parsingOwnerToken: str | None = None,
    ) -> JobSuccessFinalization:
        source: Job = db.scalars(select(Job).where(Job.job_id == jobId)).one()
        storage = validate_job_corpus(db, job=source, lock_document=False)
        metadata: dict[str, object] = dict(source.job_metadata or {})
        namespace: str = str(metadata.get("namespace") or "default")
        ownerId: str = storage.resolve_owner(str(source.user_id))
        documentId: str = str(metadata.get("document_id") or f"doc_{uuid4().hex[:12]}")
        control = LexicalCoordination.lockControl(db, requiresServing=False)
        lock_namespace_generation(db, user_id=ownerId, namespace=namespace)
        document = db.scalar(
            select(storage.Document)
            .where(storage.Document.document_id == documentId, storage.Document.user_id == ownerId)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job: Job = db.scalars(
            select(Job)
            .where(Job.job_id == jobId)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if job.parsing_status == "completed":
            return JobSuccessFinalization(
                response=JobSuccessResponse.completed(job_id=jobId, stored_count=storedCount),
                post_commit_effects=PostCommitEffectPlan.none(),
            )
        if job.status not in ("running", "converting"):
            return JobSuccessFinalization(
                response=JobSuccessResponse.state_transition_failed(job_id=jobId),
                post_commit_effects=PostCommitEffectPlan.none(),
            )
        if parsingOwnerToken is not None:
            ParsingOwnership.assertJobOwned(job, parsingOwnerToken, LexicalCoordination.getDatabaseTime(db))
        if document is not None and (
            document.status == "archived" or document.accepted_job_id not in (None, jobId)
        ):
            raise ValueError("Parsed revision is archived or superseded")
        if document is None:
            if storage.is_demo:
                raise ValueError("Demo parsing requires its admitted catalog document")
            document = storage.Document(document_id=documentId, user_id=ownerId,
                                        namespace=namespace, status="active")
            db.add(document)
        document.accepted_job_id = jobId
        metadata = dict(job.job_metadata or {})
        metadata["document_id"] = documentId
        if documentTopSummary is not None:
            metadata["document_top_summary"] = documentTopSummary
        job.job_metadata = metadata
        writer: SyncJobResultWriter = SyncJobResultWriter()
        result = writer.upsert_job_result(db, jobId, deliveryMode,
            inline_payload={"checksum": checksum}, result_s3_key=resultKey, result_size=resultSize)
        result.indexing_input_compiler = LexicalPreparation.getCompilerFingerprint()
        writer.replace_chunks(db, result.id, chunks)
        if storage.is_demo:
            result.demo_document_id = documentId
        else:
            result.document_id = documentId
        scope: DocumentPublicationScope = DocumentPublicationScope(
            user_id=ownerId, namespace=namespace, document_id=documentId,
            job_result_id=result.id, source_file_name=str(metadata.get("source_file_name") or ""),
        )
        replace_document_revision_content(db, scope=scope,
            chunks=deduplicate_chunks_by_source_path(chunks),
            section_summaries=sectionSummaries, prepare_index=False)
        if storage.is_demo:
            bind_demo_examples(db, document_id=documentId, job_result_id=result.id)
        document.latest_parsed_job_result_id = result.id
        attempt: IndexingAttempt = IndexingAttempt(jobId=jobId, revisionId=result.id, backendEpoch=control.epoch)
        db.add(attempt)
        db.flush()
        job.current_indexing_attempt_id = attempt.id
        job.parsing_status = "completed"
        job.parsing_owner_expires_at = None
        job.indexing_status = "pending"
        job.failure_stage = None
        job.status = "running"
        job.version += 1
        db.flush()
        event = SyncJobWebhookOutbox().create_event(db, job_id=jobId, event_type="job.parsed")
        return JobSuccessFinalization(
            response=JobSuccessResponse.completed(job_id=jobId, stored_count=storedCount),
            post_commit_effects=PostCommitEffectPlan(indexing_job_ids=(jobId,),
                webhook_event_ids=(event.event_id,) if event else ()),
        )
