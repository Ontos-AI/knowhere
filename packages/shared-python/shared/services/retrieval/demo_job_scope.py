"""Verify durable ingestion ownership before parsing or publishing demos."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.exceptions.domain_exceptions import PermissionDeniedException
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.job import Job
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.retrieval.corpus_storage import CorpusStorage, DEMO_NAMESPACE
from shared.services.retrieval.demo_authorization import authorize_demo_transaction


def validate_job_corpus(db: Session, *, job: Job) -> CorpusStorage:
    metadata: dict[str, object] = dict(job.job_metadata or {})
    storage: CorpusStorage = CorpusStorage.resolve_namespace(
        str(metadata.get("namespace") or "")
    )
    target: object = metadata.get("corpus_target")
    documentId: str = str(metadata.get("document_id") or "")
    if not storage.is_demo:
        if target == "DEMO" or documentId.startswith("ddoc_"):
            raise PermissionDeniedException(
                user_message="Ingestion corpus target does not match the namespace."
            )
        return storage
    authorize_demo_transaction(db, user_id=str(job.user_id))
    if (
        target != "DEMO"
        or normalize_retrieval_namespace(str(metadata.get("namespace") or ""))
        != DEMO_NAMESPACE
    ):
        raise PermissionDeniedException(
            user_message="Invalid internal demo ingestion target."
        )
    document: DemoDocument | None = db.execute(
        select(DemoDocument)
        .where(
            DemoDocument.document_id == documentId,
            DemoDocument.demo_source_id == metadata.get("demo_source_id"),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if document is None or document.status == "archived":
        raise PermissionDeniedException(
            user_message="Demo ingestion source and document do not match an active source."
        )
    return storage
