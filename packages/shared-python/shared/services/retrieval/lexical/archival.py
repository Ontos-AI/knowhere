"""End unfinished indexing when a document is archived, retaining parse output."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.lexicalbuild import LexicalBuild
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.retrieval.corpus_storage import CorpusStorage


class IndexingArchival:
    def finalize(self, db: Session, *, userId: str, documentId: str,
                 namespace: str, generation: int) -> str | None:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        document = db.scalars(select(storage.Document).where(
            storage.Document.document_id == documentId,
            storage.Document.user_id == storage.resolve_owner(userId)).with_for_update()).one()
        if document.current_indexing_build_id:
            previous: LexicalBuild | None = db.get(LexicalBuild, document.current_indexing_build_id)
            if previous is not None and previous.state == "published":
                previous.state = "retired"
                previous.retirementGeneration = generation
                previous.cleanupState = "pending"
            document.current_indexing_build_id = None
        if not document.accepted_job_id:
            return None
        job: Job = db.scalars(select(Job).where(Job.job_id == document.accepted_job_id).with_for_update()).one()
        if job.status in ("done", "failed") or job.parsing_status != "completed":
            return None
        attempt: IndexingAttempt | None = db.get(IndexingAttempt, job.current_indexing_attempt_id) if job.current_indexing_attempt_id else None
        if attempt is not None:
            attempt.state = "failed"
            attempt.ownerToken = None
            attempt.claimExpiresAt = None
            if attempt.currentBuildId:
                build: LexicalBuild | None = db.get(LexicalBuild, attempt.currentBuildId)
                if build is not None:
                    build.state = "failed"
                    build.cleanupState = "pending"
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        if not SyncStateMachineService().mark_failed_outcome(db, job.job_id,
            error_message="Indexing stopped because the document was archived.", error_code="ABORTED",
            error_details={"reason": "DOCUMENT_ARCHIVED"}).succeeded:
            return None
        event = SyncJobWebhookOutbox().create_event(db, job_id=job.job_id, event_type="job.failed")
        return event.event_id if event else None
