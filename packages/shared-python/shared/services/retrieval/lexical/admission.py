"""Bind the accepted candidate in the same transaction that admits its job."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.exceptions.domain_exceptions import ConflictException, NotFoundException
from shared.models.database.job import Job
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.precondition import IndexingPreconditionError
from shared.services.retrieval.serving_generation import lock_namespace_generation


class IndexingAdmission:
    def bind(self, db: Session, *, job: Job) -> None:
        metadata: dict[str, object] = dict(job.job_metadata or {})
        documentId: str = str(metadata.get("document_id") or "")
        if not documentId:
            raise IndexingPreconditionError("Document ingestion requires a document ID.")
        storage = validate_job_corpus(db, job=job, lock_document=False)
        ownerId: str = storage.resolve_owner(str(job.user_id))
        namespace: str = str(metadata.get("namespace") or "default")
        LexicalCoordination.lockControl(db)
        lock_namespace_generation(db, user_id=ownerId, namespace=namespace)
        document = db.scalar(select(storage.Document).where(
            storage.Document.document_id == documentId,
            storage.Document.user_id == ownerId).with_for_update())
        if document is not None and (document.namespace != namespace or document.status == "archived"):
            raise IndexingPreconditionError("Updates require an active document in the same namespace.")
        active: str | None = db.scalar(select(Job.job_id).where(Job.user_id == job.user_id,
            Job.job_metadata["document_id"].as_string() == documentId,
            Job.status.in_(("pending", "waiting-file", "running", "converting"))))
        if active is not None:
            error: ConflictException = ConflictException(user_message="This document already has an active job.",
                reason="ABORTED", resource="job", resource_id=active)
            error.details["reason"] = "DOCUMENT_JOB_ACTIVE"
            error.details["active_job_id"] = active
            raise error
        if document is None:
            if storage.is_demo:
                raise NotFoundException(resource="demo document", resource_id=documentId)
            document = storage.Document(document_id=documentId, user_id=ownerId,
                namespace=namespace, status="active")
            db.add(document)
        document.accepted_job_id = job.job_id
