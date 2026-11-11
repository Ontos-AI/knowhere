"""Stage a native build before replacing searchable progressive content."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from time import monotonic
from typing import TypeVar
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session, noload, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.document import Document, RetrievalNamespaceGeneration
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.redis.distributed_lock import RedisJobLock
from shared.services.redis.redis_sync_service import SyncRedisServiceFactory
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.hydration.build_snapshot import BuildSnapshot
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from shared.services.retrieval.lexical.compilation_checkpoint import CompilationCheckpoint
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.input_budget import LexicalInputBudget
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.manifest_publication import (
    ManifestPublication, ManifestPublicationAdvanced, PreparedManifest,
)
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.lexical.unit_metadata import persist_unit_metadata
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.scoring.knowhere_provider import SectionRow, UnitRow
from shared.services.retrieval.serving_generation import lock_namespace_generation

_Result = TypeVar("_Result")


class _ProgressiveBuildAdvanced(LexicalIndexError):
    """A peer chapter published first. Rebase the same update on its build."""


@dataclass(frozen=True)
class _Source:
    sections: tuple[SectionRow, ...]
    chunks: tuple[UnitRow, ...]


@dataclass(frozen=True)
class _Candidate:
    previousBuildId: str
    buildId: str
    leaseId: str
    source: _Source


class ProgressivePublication:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()
        self.coordination: LexicalCoordination = LexicalCoordination(self.sessions)

    @classmethod
    def publishForServingBackend(
        cls, *, scope: DocumentPublicationScope, update: Callable[[Session, DocumentPublicationScope], _Result],
        openDatabase: Callable[[], AbstractContextManager[Session]],
    ) -> _Result:
        if settings.KNOWHERE_LEXICAL_BACKEND == "opensearch":
            return cls().publish(scope=scope, update=update)
        with openDatabase() as db:
            return update(db, scope)

    def publish(self, *, scope: DocumentPublicationScope,
                update: Callable[[Session, DocumentPublicationScope], _Result]) -> _Result:
        """Apply a replayable SQL update after its exact native index is ready."""
        storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
        lexicalScope: LexicalScope = LexicalScope(ownerId=storage.resolve_owner(scope.user_id),
            namespace=scope.namespace, corpusKind="demo" if storage.is_demo else "private")
        if settings.KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS:
            try:
                return self._publish(scope, update, storage=storage, lexicalScope=lexicalScope)
            except _ProgressiveBuildAdvanced:
                return self._publish(scope, update, storage=storage, lexicalScope=lexicalScope)
        lock = RedisJobLock(SyncRedisServiceFactory.get_service(),
            f"progressive-publication:{lexicalScope.fingerprint}:{scope.document_id}")
        queuedAt: float = monotonic()
        if not lock.acquire(waitSeconds=settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS):
            raise LexicalIndexError("Timed out waiting to publish progressive content", isRetryable=True)
        try:
            logger.bind(progressive_publication_queue_ms=round((monotonic() - queuedAt) * 1000, 2)).info(
                "Progressive publication slot acquired"
            )
            try:
                return self._publish(scope, update, storage=storage, lexicalScope=lexicalScope)
            except _ProgressiveBuildAdvanced:
                # Keep the database generation fence when a Redis lease is lost.
                return self._publish(scope, update, storage=storage, lexicalScope=lexicalScope)
        finally:
            lock.release()

    def _publish(self, scope: DocumentPublicationScope,
                 update: Callable[[Session, DocumentPublicationScope], _Result], *,
                 storage: CorpusStorage, lexicalScope: LexicalScope) -> _Result:
        scope = replace(scope, row_identity_seed=uuid4(),
            prepare_pg_tokens=not settings.KNOWHERE_OPENSEARCH_PG_COPY_ON_DEMAND)
        # One budget covers the native build and publication of its source.
        deadline: IndexingDeadline = IndexingDeadline(settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS)
        native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
            scope=lexicalScope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
            maximumBulkBytes=settings.KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES, deadline=deadline)
        candidate: _Candidate | None = None
        isWriting: bool = False
        try:
            indexUuid, epoch = self.coordination.ensureIndex(scope=lexicalScope, native=native)
            with self._transaction(deadline) as db:
                _, previous, result, generation = self._lockPublication(db, scope, storage, lexicalScope, indexUuid, epoch)
                previousBuildId: str = previous.id
                attemptId: str = previous.attemptId
                jobId: str = result.job_id
                generationBefore: int = generation.generation
                preview = db.begin_nested()
                outcome: _Result = update(db, scope)
                if generation.generation == generationBefore:
                    preview.commit()
                    return outcome
                source: _Source = self._readSource(db, scope, storage)
                preview.rollback()
                expiresAt = self.coordination.getDatabaseTime(db) + timedelta(seconds=deadline.getRemainingSeconds())
                build: LexicalBuild = LexicalBuild(attemptId=attemptId, namespaceIndexId=lexicalScope.fingerprint,
                    documentId=scope.document_id, revisionId=scope.job_result_id, ownerToken=str(uuid4()),
                    cleanupState="pending", nextMaintenanceAt=expiresAt)
                db.add(build)
                db.flush()
                lease: LexicalLease = LexicalLease(namespaceIndexId=lexicalScope.fingerprint,
                    kind="index_use", ownerToken=build.ownerToken, backendEpoch=epoch, buildId=build.id, expiresAt=expiresAt)
                db.add(lease)
                db.flush()
                candidate = _Candidate(previousBuildId, build.id, lease.id, source)

            with deadline.enforce():
                payload: LexicalInput = LexicalPreparation.prepare(documentId=scope.document_id,
                    revisionId=scope.job_result_id, sections=candidate.source.sections, chunks=candidate.source.chunks,
                    checkpoint=CompilationCheckpoint(deadline).check)
                key, checksum, byteCount = LexicalInputStorage().saveWithSize(jobId=jobId,
                    revisionId=scope.job_result_id, payload=payload)
                if native.getIndexUuid() != indexUuid:
                    raise LexicalIndexError("Native index changed before progressive write")
                isWriting = True
                native.writeBuild(buildId=candidate.buildId, units=payload.units)
                isWriting = False
                if native.getIndexUuid() != indexUuid:
                    raise LexicalIndexError("Native index changed before progressive publication")

            publication = ManifestPublication(self.sessions)
            for _ in range(8):
                with deadline.enforce():
                    prepared = publication.prepare(scope=lexicalScope, documentId=scope.document_id,
                        buildId=candidate.buildId, native=native)
                try:
                    return self._activate(scope, update, storage, lexicalScope, indexUuid, epoch,
                        candidate, payload, key, checksum, byteCount, deadline, prepared)
                except ManifestPublicationAdvanced:
                    continue
            raise ManifestPublicationAdvanced("Namespace manifest kept advancing during refinement", isRetryable=True)
        except Exception as error:
            if candidate is not None:
                self._abandon(candidate, isUncertain=isWriting and isinstance(error, LexicalIndexError) and error.hasUnknownOutcome)
            raise
        finally:
            native.close()

    def _activate(
        self, scope: DocumentPublicationScope, update: Callable[[Session, DocumentPublicationScope], _Result],
        storage: CorpusStorage, lexicalScope: LexicalScope, indexUuid: str, epoch: int,
        candidate: _Candidate, payload: LexicalInput, key: str, checksum: str, byteCount: int,
        deadline: IndexingDeadline, prepared: PreparedManifest | None,
    ) -> _Result:
        with self._transaction(deadline) as db:
            document, previous, result, generation = self._lockPublication(
                db, scope, storage, lexicalScope, indexUuid, epoch)
            if previous.id != candidate.previousBuildId:
                raise _ProgressiveBuildAdvanced("Another progressive stage published first", isRetryable=True)
            build = db.get(LexicalBuild, candidate.buildId, with_for_update=True)
            lease = db.get(LexicalLease, candidate.leaseId, with_for_update=True)
            now = self.coordination.getDatabaseTime(db)
            if (build is None or build.state != "writing" or lease is None or lease.state != "active"
                or lease.expiresAt <= now or lease.ownerToken != build.ownerToken or lease.backendEpoch != epoch):
                raise LexicalIndexError("Progressive publication lost its build authority", isRetryable=True)
            if prepared is not None:
                BuildSnapshot.capture(db, scope=lexicalScope, buildId=previous.id)
            # A copy queued for the previous build may still be running.
            db.execute(select(func.pg_advisory_xact_lock(LegacyLexicalCopy.getRevisionLockIdentity(scope.job_result_id))))
            generationBefore = generation.generation
            outcome = update(db, scope)
            if generation.generation == generationBefore or self._readSource(db, scope, storage) != candidate.source:
                raise LexicalIndexError("Progressive content changed after index preparation", isRetryable=True)
            if not scope.prepare_pg_tokens:
                persist_unit_metadata(db, storage=storage, document_id=scope.document_id,
                    revision_id=scope.job_result_id, payload=payload, replace_existing=True)
            LexicalInputRetention.assertRetained(db, result, key=key)
            result.indexing_input_key = key
            result.indexing_input_checksum = checksum
            result.indexing_input_compiler = payload.compilerFingerprint
            result.indexing_input_bytes = byteCount
            document.current_indexing_build_id = build.id
            previous.state = "retired"
            previous.retirementGeneration = generation.generation
            previous.cleanupState = "pending"
            previous.retainForManifestReads = prepared is not None
            if previous.legacyCopyState == "pending":
                previous.legacyCopyState = "skipped"
            build.state = "published"
            if prepared is not None:
                BuildSnapshot.capture(db, scope=lexicalScope, buildId=build.id)
            ManifestPublication.activate(db, prepared, previousGeneration=generationBefore,
                publishedGeneration=generation.generation)
            build.unitCount = len(payload.units)
            build.activationGeneration = generation.generation
            build.cleanupState = "not_started"
            build.legacyCopyState = "completed" if scope.prepare_pg_tokens else "skipped"
            build.nextMaintenanceAt = None
            lease.state = "released"
        return outcome

    def _lockPublication(
        self, db: Session, scope: DocumentPublicationScope, storage: CorpusStorage,
        lexicalScope: LexicalScope, indexUuid: str, epoch: int,
    ) -> tuple[Document | DemoDocument, LexicalBuild, JobResult, RetrievalNamespaceGeneration]:
        if storage.is_demo:
            authorize_demo_transaction(db, user_id=scope.user_id)
        control = self.coordination.lockControl(db)
        generation = lock_namespace_generation(db, user_id=scope.user_id, namespace=scope.namespace)
        registry = db.get(LexicalNamespaceIndex, lexicalScope.fingerprint, with_for_update={"read": True})
        document = db.scalar(select(storage.Document).where(storage.Document.document_id == scope.document_id,
            storage.Document.user_id == lexicalScope.ownerId, storage.Document.namespace == scope.namespace).with_for_update())
        if (control.epoch != epoch or registry is None or registry.state != "open" or registry.indexUuid != indexUuid
            or document is None or document.status != "active" or document.current_job_result_id != scope.job_result_id
            or not document.current_indexing_build_id):
            raise LexicalIndexError("Progressive publication lost its current revision or native index", isRetryable=True)
        previous = db.get(LexicalBuild, document.current_indexing_build_id, with_for_update=True)
        if (previous is None or previous.state != "published" or previous.namespaceIndexId != lexicalScope.fingerprint
            or previous.revisionId != scope.job_result_id or previous.documentId != scope.document_id):
            raise LexicalIndexError("Progressive publication has no matching published build")
        result: JobResult = db.scalars(select(JobResult).options(noload("*")).where(
            JobResult.id == scope.job_result_id).with_for_update(of=JobResult)).one()
        LexicalInputRetention.assertRetained(db, result)
        return document, previous, result, generation

    @staticmethod
    def _readSource(db: Session, scope: DocumentPublicationScope, storage: CorpusStorage) -> _Source:
        db.flush()
        LexicalInputBudget.assertCanHydrate(db, storage=storage, documentId=scope.document_id, revisionId=scope.job_result_id)
        section = storage.DocumentSection
        chunk = storage.DocumentChunk
        sections: tuple[SectionRow, ...] = tuple(
            SectionRow(row.section_id, row.parent_section_id, row.section_path, row.section_title or "",
                row.section_level, row.summary or "", row.sort_order)
            for row in db.execute(select(section.section_id, section.parent_section_id, section.section_path,
                section.section_title, section.section_level, section.summary, section.sort_order)
                .where(section.document_id == scope.document_id, section.job_result_id == scope.job_result_id)
                .order_by(section.sort_order, section.section_id)))
        chunks: tuple[UnitRow, ...] = tuple(
            UnitRow(row.chunk_id, row.section_id, row.chunk_type, row.content or "", row.sort_order,
                row.source_chunk_path or "", row.file_path or "", dict(row.chunk_metadata or {}))
            for row in db.execute(select(chunk.chunk_id, chunk.section_id, chunk.chunk_type, chunk.content,
                chunk.sort_order, chunk.source_chunk_path, chunk.file_path, chunk.chunk_metadata)
                .where(chunk.document_id == scope.document_id, chunk.job_result_id == scope.job_result_id)
                .order_by(chunk.sort_order, chunk.chunk_id, chunk.id)))
        return _Source(sections, chunks)

    def _abandon(self, candidate: _Candidate, *, isUncertain: bool) -> None:
        try:
            with self._transaction(IndexingDeadline(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)) as db:
                build = db.get(LexicalBuild, candidate.buildId, with_for_update=True)
                lease = db.get(LexicalLease, candidate.leaseId, with_for_update=True)
                if build is not None and build.state == "writing":
                    build.state = "failed"
                    build.nextMaintenanceAt = None
                if lease is not None and lease.state == "active":
                    lease.state = "uncertain" if isUncertain else "released"
        except Exception as error:
            logger.warning("Progressive build settlement deferred: build_id={}, error_type={}",
                candidate.buildId, type(error).__name__)

    @contextmanager
    def _transaction(self, deadline: IndexingDeadline) -> Iterator[Session]:
        with self.sessions.begin() as db:
            timeout: str = f"{max(1, int(deadline.getRemainingSeconds() * 1000))}ms"
            db.execute(select(func.set_config("statement_timeout", timeout, True), func.set_config("lock_timeout", timeout, True)))
            yield db
            deadline.getRemainingSeconds()
