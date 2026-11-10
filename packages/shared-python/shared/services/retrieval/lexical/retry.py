"""Create one free indexing attempt while preserving parsed results and charges."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import ConflictException, NotFoundException
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.precondition import IndexingPreconditionError
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.serving_generation import lock_namespace_generation


class IndexingRetry:
    def request(self, db: Session, *, jobId: str, userId: str, deadline: IndexingDeadline | None = None) -> bool:
        deadline = deadline or IndexingDeadline(settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS
            if settings.KNOWHERE_LEXICAL_BACKEND == "opensearch" else settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS)
        timeout: str = f"{max(1, int(deadline.getRemainingSeconds() * 1000))}ms"
        db.execute(select(func.set_config("statement_timeout", timeout, True), func.set_config("lock_timeout", timeout, True)))
        source: Job | None = db.scalar(select(Job).where(Job.job_id == jobId, Job.user_id == userId))
        if source is None:
            raise NotFoundException(resource="job", resource_id=jobId)
        if source.status == "done":
            return False
        storage = validate_job_corpus(db, job=source, lock_document=False)
        metadata: dict[str, object] = dict(source.job_metadata or {})
        control = LexicalCoordination.lockControl(db)
        lock_namespace_generation(db, user_id=storage.resolve_owner(userId),
                                  namespace=str(metadata.get("namespace") or "default"))
        document = db.scalar(select(storage.Document).where(
            storage.Document.document_id == str(metadata.get("document_id") or ""),
            storage.Document.user_id == storage.resolve_owner(userId)).with_for_update().execution_options(populate_existing=True))
        job: Job = db.scalars(select(Job).where(Job.job_id == jobId).with_for_update().execution_options(populate_existing=True)).one()
        if job.status == "done":
            return False
        if document is None or document.status == "archived" or job.parsing_status != "completed":
            raise IndexingPreconditionError("Indexing retry requires an active document with a completed parse.")
        if document.accepted_job_id != jobId:
            error: ConflictException = ConflictException(
                user_message="A newer job has replaced this indexing candidate.", reason="ABORTED",
                resource="job", resource_id=jobId)
            error.details["reason"] = "INDEXING_SUPERSEDED"
            raise error
        if job.indexing_status in ("pending", "running"):
            activeError: ConflictException = ConflictException(user_message="Indexing is already running for this job.",
                reason="ABORTED", resource="job", resource_id=jobId)
            activeError.details["reason"] = "INDEXING_ALREADY_RUNNING"
            raise activeError
        if job.status != "failed" or job.failure_stage != "indexing":
            raise IndexingPreconditionError("Only a failed indexing phase can be retried independently.")
        result: JobResult | None = db.scalar(select(JobResult).where(JobResult.job_id == jobId))
        if result is None:
            raise IndexingPreconditionError("The parsed input is no longer available.")
        active = select(Job).where(Job.job_id != jobId, Job.status.notin_(("done", "failed")),
            Job.job_metadata["document_id"].as_string() == document.document_id,
            func.coalesce(Job.job_metadata["namespace"].as_string(), "default") == document.namespace)
        if not storage.is_demo:
            active = active.where(Job.user_id == userId)
        otherJob: Job | None = db.scalar(active.order_by(Job.created_at.desc()).limit(1))
        if otherJob is not None:
            documentError: ConflictException = ConflictException(user_message="Another document job is still active.",
                reason="ABORTED", resource="document", resource_id=document.document_id)
            documentError.details["reason"] = "DOCUMENT_JOB_ACTIVE"
            if str(otherJob.user_id) == userId:
                documentError.details["active_job"] = {"job_id": otherJob.job_id, "status": otherJob.status}
            raise documentError
        number: int = int(db.scalar(select(func.max(IndexingAttempt.attemptNumber))
            .where(IndexingAttempt.jobId == jobId)) or 0) + 1
        attempt: IndexingAttempt = IndexingAttempt(jobId=jobId, revisionId=result.id,
            attemptNumber=number, backendEpoch=control.epoch, origin="manual",
            admissionDeadlineAt=LexicalCoordination.getDatabaseTime(db)
            + timedelta(seconds=deadline.getRemainingSeconds()))
        db.add(attempt)
        db.flush()
        if not SyncStateMachineService().transition_outcome(
            db, jobId, "pending", transition_reason="indexing_retry", operator_id=userId,
            operator_type="user").succeeded:
            raise IndexingPreconditionError("The job cannot accept an indexing retry in its current state.")
        job.current_indexing_attempt_id = attempt.id
        job.indexing_status = "pending"
        job.failure_stage = None
        job.error_message = None
        job.error_code = None
        job.job_metadata = {key: value for key, value in metadata.items() if key != "error"}
        db.flush()
        return True
