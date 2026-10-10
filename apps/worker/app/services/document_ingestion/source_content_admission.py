from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import CompoundSelect, literal, or_, select, text
from sqlalchemy.orm import Session

from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
)
from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace

_HASH_FIELD: str = "source_content_sha256"
_SIZE_FIELD: str = "source_size_bytes"
_ACTIVE_JOB_STATUSES: tuple[str, ...] = (
    "waiting-file",
    "pending",
    "running",
    "converting",
)


@dataclass(frozen=True)
class _SourceScope:
    user_id: str
    namespace: str
    is_demo: bool


class SourceContentAdmission:
    """Reject identical original bytes within a user's active document scope.

    Fingerprints live on the source Job, so joining the current Job Result
    compares only active revisions. Sources without fingerprints are ignored.
    A transaction lock serializes content claims before parsing starts; running
    Jobs reserve their content until they publish or become terminal. Both reads
    use one statement snapshot to cover the transition from running to published.
    """

    def admit_source(self, *, job_id: str, local_file_path: str) -> dict[str, object]:
        with Path(local_file_path).open("rb") as source_file:
            digest: str = hashlib.file_digest(source_file, "sha256").hexdigest()
        size: int = Path(local_file_path).stat().st_size
        fingerprint: dict[str, object] = {_HASH_FIELD: digest, _SIZE_FIELD: size}
        scope: _SourceScope = self._load_scope(job_id)
        with get_sync_db_context() as database:
            if not scope.is_demo:
                self._lock_content_claim(database, scope, digest)
                self._reject_existing_content(database, scope, digest, job_id)
            self._save_fingerprint(database, job_id, fingerprint)
        return fingerprint

    def _load_scope(self, job_id: str) -> _SourceScope:
        with get_sync_db_context() as database:
            job: Job | None = database.get(Job, job_id)
            if job is None:
                raise NotFoundException(resource="Job", resource_id=job_id)
            metadata: dict[str, object] = dict(job.job_metadata or {})
            namespace: object = metadata.get("namespace")
            return _SourceScope(
                user_id=job.user_id,
                namespace=normalize_retrieval_namespace(
                    namespace if isinstance(namespace, str) else None,
                ),
                is_demo=metadata.get("corpus_target") == "DEMO",
            )

    @staticmethod
    def _lock_content_claim(
        database: Session, scope: _SourceScope, digest: str
    ) -> None:
        identity: str = json.dumps(
            ["source-content", scope.user_id, scope.namespace, digest]
        )
        lock_key: int = int.from_bytes(
            hashlib.sha256(identity.encode("utf-8")).digest()[:8],
            "big",
            signed=True,
        )
        database.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    def _reject_existing_content(
        self,
        database: Session,
        scope: _SourceScope,
        digest: str,
        job_id: str,
    ) -> None:
        match: tuple[str, str] | None = (
            database.execute(self._build_duplicate_query(scope, digest, job_id))
            .tuples()
            .first()
        )
        if match is None:
            return
        resource, resource_id = match
        user_message: str = (
            "A document with identical file content already exists in this namespace."
            if resource == "Document"
            else "A job with identical file content is already being processed in this namespace."
        )
        raise ConflictException(
            user_message=user_message,
            resource=resource,
            resource_id=resource_id,
            internal_message=f"Duplicate source content: resource={resource} id={resource_id}",
        )

    @staticmethod
    def _build_duplicate_query(
        scope: _SourceScope,
        digest: str,
        job_id: str,
    ) -> CompoundSelect[tuple[str, str]]:
        return (
            select(literal("Document"), Document.document_id)
            .select_from(Job)
            .join(JobResult, JobResult.job_id == Job.job_id)
            .join(Document, Document.current_job_result_id == JobResult.id)
            .where(Job.user_id == scope.user_id)
            .where(Job.job_metadata[_HASH_FIELD].as_string() == digest)
            .where(Document.user_id == scope.user_id)
            .where(Document.namespace == scope.namespace)
            .where(Document.status == "active")
            .union_all(
                select(literal("Job"), Job.job_id)
                .where(Job.user_id == scope.user_id)
                .where(Job.job_id != job_id)
                .where(Job.status.in_(_ACTIVE_JOB_STATUSES))
                .where(Job.job_metadata["namespace"].as_string() == scope.namespace)
                .where(Job.job_metadata[_HASH_FIELD].as_string() == digest)
                .where(
                    or_(
                        Job.job_metadata["corpus_target"].as_string().is_(None),
                        Job.job_metadata["corpus_target"].as_string() != "DEMO",
                    )
                )
            )
            .limit(1)
        )

    @staticmethod
    def _save_fingerprint(
        database: Session,
        job_id: str,
        fingerprint: dict[str, object],
    ) -> None:
        job: Job | None = database.execute(
            select(Job).where(Job.job_id == job_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundException(resource="Job", resource_id=job_id)
        job.job_metadata = {**dict(job.job_metadata or {}), **fingerprint}
