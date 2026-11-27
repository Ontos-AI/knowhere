"""Reference-aware input collection with durable fences and bounded inventory."""

from datetime import datetime, timedelta
import re

from loguru import logger
from sqlalchemy import Select, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, noload, sessionmaker

from shared.core.config.storage import get_cached_storage_adapter
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.document import Document
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalinputinventory import LexicalInputInventory
from shared.models.database.lexicallease import LexicalLease
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.storage.object_inventory_page import ObjectInventoryPage

_INPUT_PATTERN: re.Pattern[str] = re.compile(r"indexing-input/v1/([^/]{1,36})/([^/]{1,36})/[a-f0-9]{64}(?:-[a-f0-9]{32})?\.json\.zlib")
_SCAN_INTERVAL: timedelta = timedelta(minutes=5)
# An input can become current between the operator's inspection and collection.
_CURRENT_INPUT_RECHECK_INTERVAL: timedelta = timedelta(days=1)
_ORPHAN_GRACE: timedelta = timedelta(seconds=7200)
_MAXIMUM_SCAN: int = 100
_CURSOR_KEY: str = "__indexing_input_cursor__"
_Revision = tuple[JobResult, Job, Document | DemoDocument | None]


class LexicalInputCollector:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()
        self.bucket: str = LexicalInputStorage().bucket

    def findCandidates(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Inspect due revisions and inventoried orphans in one read-only snapshot."""
        assigned: list[str] = []
        orphans: list[str] = []
        with self.sessions.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            now: datetime = LexicalCoordination.getDatabaseTime(db)
            assignedQuery: Select[tuple[str]] = (
                select(JobResult.id)
                .where(
                    JobResult.indexing_input_key.is_not(None),
                    JobResult.indexing_input_gc_state != "deleted",
                    or_(JobResult.indexing_input_next_scan_at.is_(None), JobResult.indexing_input_next_scan_at <= now),
                    ~select(Document.document_id).where(
                        Document.current_job_result_id == JobResult.id, Document.status == "active",
                    ).exists(),
                    ~select(DemoDocument.document_id).where(
                        DemoDocument.current_job_result_id == JobResult.id, DemoDocument.status == "active",
                    ).exists(),
                )
                .order_by(JobResult.indexing_input_next_scan_at.asc().nullsfirst(), JobResult.id)
            )
            for revisionId in db.scalars(assignedQuery).all():
                revision: _Revision | None = self._loadRevision(db, revisionId, lock=False)
                if revision is None:
                    continue
                result, job, document = revision
                key: str | None = result.indexing_input_key
                if (key is not None and not self._isCurrentRevision(result, document)
                    and not self._hasConsumer(db, result, job, document, includeRetry=True)
                    and not self._hasReference(db, key, excludingRevision=revisionId)):
                    assigned.append(revisionId)

            orphanQuery: Select[tuple[str]] = (
                select(LexicalInputInventory.objectKey)
                .where(
                    LexicalInputInventory.bucket == self.bucket,
                    LexicalInputInventory.state.in_(("observed", "deleting")),
                    LexicalInputInventory.nextScanAt <= now,
                    or_(LexicalInputInventory.state == "deleting", LexicalInputInventory.firstSeenAt <= now - _ORPHAN_GRACE),
                    ~select(JobResult.id).where(
                        JobResult.indexing_input_key == LexicalInputInventory.objectKey,
                        JobResult.indexing_input_gc_state != "deleted",
                    ).exists(),
                )
                .order_by(LexicalInputInventory.nextScanAt, LexicalInputInventory.id)
            )
            for key in db.scalars(orphanQuery).all():
                match: re.Match[str] | None = _INPUT_PATTERN.fullmatch(key)
                if match is not None and self._isOrphanUnused(db, key, match[2], lock=False):
                    orphans.append(key)
        return tuple(assigned), tuple(orphans)

    def discover(self, *, isImmediate: bool = False) -> ObjectInventoryPage | None:
        """One persisted key cursor and at most one remote listing request.

        ``isImmediate`` lets an operator page through the bucket without waiting
        for the cursor's next scan time.
        """
        identity: str = LexicalInputRetention.getIdentity(bucket=self.bucket, key=_CURSOR_KEY)
        with self.sessions.begin() as db:
            now: datetime = LexicalCoordination.getDatabaseTime(db)
            db.execute(insert(LexicalInputInventory).values(id=identity, bucket=self.bucket,
                object_key=_CURSOR_KEY, state="cursor", first_seen_at=now, next_scan_at=now)
                .on_conflict_do_nothing(index_elements=["id"]))
            cursor: LexicalInputInventory | None = db.scalar(select(LexicalInputInventory).where(
                LexicalInputInventory.id == identity).with_for_update(skip_locked=True))
            if cursor is None or (not isImmediate and cursor.nextScanAt > now):
                return None
            after: str | None = cursor.scanAfter
            cursor.nextScanAt = now + _SCAN_INTERVAL
        try:
            page: ObjectInventoryPage = get_cached_storage_adapter().list_object_page(
                prefix="indexing-input/v1/", after=after, limit=_MAXIMUM_SCAN, bucket=self.bucket)
        except Exception as error:
            self._recordFailure(_CURSOR_KEY, None, error)
            raise
        with self.sessions.begin() as db:
            now = LexicalCoordination.getDatabaseTime(db)
            cursor = db.get(LexicalInputInventory, identity, with_for_update=True)
            if cursor is None or cursor.scanAfter != after:
                return None
            for key, size in page.objects:
                match: re.Match[str] | None = _INPUT_PATTERN.fullmatch(key)
                if match is None:
                    logger.warning("Indexing input inventory found an unsupported object name")
                    continue
                objectIdentity: str = LexicalInputRetention.getIdentity(bucket=self.bucket, key=key)
                item: LexicalInputInventory | None = db.get(LexicalInputInventory, objectIdentity, with_for_update=True)
                if item is None:
                    db.add(LexicalInputInventory(id=objectIdentity, bucket=self.bucket, objectKey=key,
                        jobId=match[1], revisionId=match[2], sizeBytes=size,
                        firstSeenAt=now, nextScanAt=now, state="observed"))
                else:
                    item.sizeBytes = size
                    if item.state == "deleted":
                        # A delayed old uploader may recreate bytes, never a
                        # valid reference: keep the tombstone and delete again.
                        item.state = "deleting"
                        item.nextScanAt = now
            cursor.scanAfter = page.nextCursor
            cursor.nextScanAt = now + _SCAN_INTERVAL
            cursor.lastError = None
        return page

    def collectAssigned(self, revisionId: str) -> bool:
        key: str | None = None
        try:
            with self.sessions.begin() as db:
                locked: _Revision | None = self._loadRevision(db, revisionId, lock=True)
                if locked is None:
                    return False
                result, job, document = locked
                key = result.indexing_input_key
                if key is None or result.indexing_input_gc_state == "deleted":
                    return False
                now: datetime = LexicalCoordination.getDatabaseTime(db)
                result.indexing_input_next_scan_at = now + _SCAN_INTERVAL
                # Backend verification and rollback both read the current published input.
                if self._isCurrentRevision(result, document):
                    result.indexing_input_next_scan_at = now + _CURRENT_INPUT_RECHECK_INTERVAL
                    return False
                if self._hasConsumer(db, result, job, document, includeRetry=True):
                    return False
                if self._hasReference(db, key, excludingRevision=revisionId):
                    raise LexicalIndexError("Indexing input has another unresolved reference")
                size: int | None = result.indexing_input_bytes
                item: LexicalInputInventory = self._lockObject(db, key, now)
                result.indexing_input_gc_state = "deleting"
                result.indexing_input_gc_attempts += 1
                item.state = "deleting"
                item.attempts += 1
                item.nextScanAt = now + _SCAN_INTERVAL
            if size is None:
                size = get_cached_storage_adapter().get_object_size(key, bucket=self.bucket)
            self._deleteObject(key)
            with self.sessions.begin() as db:
                result = db.scalar(select(JobResult).options(noload("*")).where(JobResult.id == revisionId).with_for_update(of=JobResult))
                if result is not None and result.indexing_input_key == key and result.indexing_input_gc_state == "deleting":
                    result.indexing_input_gc_state = "deleted"
                    result.indexing_input_gc_error = None
                    result.indexing_input_bytes = size
                item = self._lockObject(db, key, LexicalCoordination.getDatabaseTime(db))
                item.state = "deleted"
                item.sizeBytes = size
                item.lastError = None
            return True
        except Exception as error:
            self._recordFailure(key, revisionId, error)
            return False

    def collectOrphan(self, key: str) -> bool:
        match: re.Match[str] | None = _INPUT_PATTERN.fullmatch(key)
        if match is None:
            return False
        revisionId: str = match[2]
        try:
            with self.sessions.begin() as db:
                if not self._isOrphanUnused(db, key, revisionId, lock=True):
                    return False
                now: datetime = LexicalCoordination.getDatabaseTime(db)
                item: LexicalInputInventory = self._lockObject(db, key, now)
                item.nextScanAt = now + _SCAN_INTERVAL
                if item.state == "deleted":
                    return False
                if item.state != "deleting" and now - item.firstSeenAt < _ORPHAN_GRACE:
                    return False
                item.state = "deleting"
                item.attempts += 1
            self._deleteObject(key)
            with self.sessions.begin() as db:
                item = self._lockObject(db, key, LexicalCoordination.getDatabaseTime(db))
                item.state = "deleted"
                item.lastError = None
            return True
        except Exception as error:
            self._recordFailure(key, None, error)
            return False

    def _isOrphanUnused(self, db: Session, key: str, revisionId: str, *, lock: bool) -> bool:
        revision: _Revision | None = self._loadRevision(db, revisionId, lock=lock)
        # A busy document or job can prevent a revision from being locked.
        if revision is None and db.scalar(select(JobResult.id).where(JobResult.id == revisionId)) is not None:
            return False
        if self._hasReference(db, key):
            return False
        return revision is None or not self._hasConsumer(db, *revision, includeRetry=False)

    @staticmethod
    def _hasReference(db: Session, key: str, *, excludingRevision: str | None = None) -> bool:
        query: Select[tuple[str]] = select(JobResult.id).where(
            JobResult.indexing_input_key == key, JobResult.indexing_input_gc_state != "deleted")
        if excludingRevision is not None:
            query = query.where(JobResult.id != excludingRevision)
        return db.scalar(query.limit(1)) is not None

    @staticmethod
    def _isCurrentRevision(result: JobResult, document: Document | DemoDocument | None) -> bool:
        return document is not None and document.status == "active" and document.current_job_result_id == result.id

    def _loadRevision(self, db: Session, revisionId: str, *, lock: bool) -> _Revision | None:
        result: JobResult | None = db.scalar(select(JobResult).options(noload("*")).where(JobResult.id == revisionId))
        if result is None:
            return None
        source: Job | None = db.scalar(select(Job).options(noload("*")).where(Job.job_id == result.job_id))
        if source is None:
            return None
        privateDocumentId: str | None = result.document_id
        demoDocumentId: str | None = result.demo_document_id
        if demoDocumentId is not None:
            storage: CorpusStorage = CorpusStorage("DEMO")
            documentId: str = demoDocumentId
        elif privateDocumentId is not None:
            storage = CorpusStorage("PRIVATE")
            documentId = privateDocumentId
        else:
            metadata: dict[str, object] = dict(source.job_metadata or {})
            storage = CorpusStorage.resolve_namespace(str(metadata.get("namespace") or "default"))
            documentId = str(metadata.get("document_id") or "")
        if storage.is_demo:
            authorize_demo_transaction(db, user_id=source.user_id)
        documentQuery: Select[tuple[Document | DemoDocument]] = select(storage.Document).where(
            storage.Document.document_id == documentId, storage.Document.user_id == storage.resolve_owner(source.user_id))
        if not lock:
            return result, source, db.scalar(documentQuery)
        exists: str | None = db.scalar(select(storage.Document.document_id).where(
            storage.Document.document_id == documentId, storage.Document.user_id == storage.resolve_owner(source.user_id)))
        document: Document | DemoDocument | None = db.scalar(
            documentQuery.with_for_update(skip_locked=True)) if exists is not None else None
        if exists is not None and document is None:
            return None
        job: Job | None = db.scalar(select(Job).options(noload("*")).where(Job.job_id == source.job_id)
            .with_for_update(of=Job, skip_locked=True).execution_options(populate_existing=True))
        result = db.scalar(select(JobResult).options(noload("*")).where(JobResult.id == revisionId)
            .with_for_update(of=JobResult, skip_locked=True).execution_options(populate_existing=True))
        if result is None or job is None or (result.document_id, result.demo_document_id) != (privateDocumentId, demoDocumentId):
            return None
        return result, job, document

    @staticmethod
    def _hasConsumer(db: Session, result: JobResult, job: Job, document: Document | DemoDocument | None, *, includeRetry: bool) -> bool:
        if db.scalar(select(IndexingAttempt.id).where(IndexingAttempt.revisionId == result.id,
            IndexingAttempt.state.in_(("pending", "running"))).limit(1)) is not None:
            return True
        if db.scalar(select(LexicalLease.id).join(LexicalBuild, LexicalBuild.id == LexicalLease.buildId).where(
            LexicalBuild.revisionId == result.id, LexicalLease.state != "released").limit(1)) is not None:
            return True
        if db.scalar(select(LexicalBuild.id).where(LexicalBuild.revisionId == result.id,
            LexicalBuild.legacyCopyState.in_(("pending", "running"))).limit(1)) is not None:
            return True
        return bool(includeRetry and document is not None and document.status == "active"
            and document.accepted_job_id == job.job_id
            and job.parsing_status == "completed" and (result.result_s3_key or result.inline_payload)
            and job.status == "failed" and job.failure_stage == "indexing")

    def _lockObject(self, db: Session, key: str, now: datetime) -> LexicalInputInventory:
        identity: str = LexicalInputRetention.getIdentity(bucket=self.bucket, key=key)
        db.execute(insert(LexicalInputInventory).values(id=identity, bucket=self.bucket,
            object_key=key, state="observed", first_seen_at=now, next_scan_at=now)
            .on_conflict_do_nothing(index_elements=["id"]))
        return db.scalars(select(LexicalInputInventory).where(LexicalInputInventory.id == identity).with_for_update()).one()

    def _deleteObject(self, key: str) -> None:
        if not get_cached_storage_adapter().delete_object(key, bucket=self.bucket):
            if get_cached_storage_adapter().exists(key, bucket=self.bucket):
                raise LexicalIndexError("Indexing input deletion was not acknowledged", isRetryable=True)

    def _recordFailure(self, key: str | None, revisionId: str | None, error: Exception) -> None:
        logger.warning("Indexing input collection deferred: revision_id={} error_type={}", revisionId, type(error).__name__)
        try:
            with self.sessions.begin() as db:
                now: datetime = LexicalCoordination.getDatabaseTime(db)
                if revisionId is not None:
                    result: JobResult | None = db.scalar(select(JobResult).options(noload("*")).where(JobResult.id == revisionId).with_for_update(of=JobResult))
                    if result is not None:
                        result.indexing_input_gc_error = type(error).__name__
                        result.indexing_input_next_scan_at = now + _SCAN_INTERVAL
                if key is not None:
                    item: LexicalInputInventory = self._lockObject(db, key, now)
                    item.lastError = type(error).__name__
                    item.nextScanAt = now + _SCAN_INTERVAL
        except Exception as persistenceError:
            logger.error("Indexing input cleanup warning could not persist: error_type={}", type(persistenceError).__name__)
