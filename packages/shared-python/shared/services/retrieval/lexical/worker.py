"""Claim durable indexing intent, write outside SQL, and activate atomically."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.document import Document, RetrievalNamespaceGeneration
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.hydration.build_snapshot import BuildSnapshot
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.compilation_checkpoint import CompilationCheckpoint
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.input_budget import LexicalInputBudget
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.manifest_publication import (
    ManifestPublication, ManifestPublicationAdvanced, PreparedManifest,
)
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.settlement import IndexingSettlement
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.lexical.unit_metadata import persist_unit_metadata
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.scoring.knowhere_provider import SectionRow, UnitRow
from shared.services.retrieval.serving_generation import lock_namespace_generation
from shared.services.retrieval.serving_manifest import persist_revision_serving_state


@dataclass(frozen=True)
class _Context:
    jobId: str
    revisionId: str
    documentId: str
    attemptId: str
    scope: LexicalScope
    actorId: str


@dataclass(frozen=True)
class _Claim:
    context: _Context
    buildId: str
    ownerToken: str
    leaseId: str
    epoch: int
    indexUuid: str


@dataclass(frozen=True)
class _InputSource:
    key: str | None
    checksum: str | None
    compiler: str | None
    sections: tuple[SectionRow, ...]
    chunks: tuple[UnitRow, ...]


@dataclass(frozen=True)
class _LockedClaim:
    job: Job
    document: Document | DemoDocument
    attempt: IndexingAttempt
    build: LexicalBuild
    lease: LexicalLease
    now: datetime
    generation: RetrievalNamespaceGeneration


class IndexingWorker:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()
        self.coordination: LexicalCoordination = LexicalCoordination(self.sessions)
        self._deadline: IndexingDeadline | None = None

    def run(self, jobId: str, *, initialOnly: bool = False) -> Literal["completed", "pending", "failed", "skipped"]:
        self._deadline = IndexingDeadline(self._getExecutionTimeout())
        context: _Context | str | None = None
        native: OpenSearchLexicalIndex | None = None
        claim: _Claim | None = None
        ownerToken: str | None = None
        settlementDeadline: IndexingDeadline | None = None
        isKnown: bool = False
        try:
            with self._deadline.enforce():
                context = self._readContext(jobId)
                if isinstance(context, str):
                    return "completed" if context == "done" else "failed" if context == "failed" else "skipped"
                if initialOnly:
                    with self._transaction() as db:
                        attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(IndexingAttempt.id == context.attemptId)).one()
                        if attempt.origin != "initial" or attempt.attemptNumber != 1:
                            return "skipped"
                with self._transaction() as db:
                    settled, eventId = IndexingSettlement.settleExpired(db, context.jobId, attemptId=context.attemptId)
                SyncJobWebhookOutbox().enqueue_event_id_after_commit(eventId)
                if settled is not None:
                    return settled
                ownerToken = self._startExecution(context)
                if ownerToken is None:
                    return "pending"
                if settings.KNOWHERE_LEXICAL_BACKEND == "postgresql":
                    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
                    publication = PostgresqlPublication(sessionFactory=self.sessions, deadline=self._deadline)
                    completed = publication.run(context, ownerToken=ownerToken)
                    SyncJobWebhookOutbox().enqueue_event_id_after_commit(completed.eventId)
                    return "completed"
                native = OpenSearchLexicalIndex(
                    endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=context.scope,
                    indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
                    maximumBulkBytes=settings.KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES,
                    deadline=self._deadline,
                )
                self._deadline.getRemainingSeconds()
                indexUuid, epoch = self.coordination.ensureIndex(scope=context.scope, native=native)
                claim = self._claim(context, ownerToken=ownerToken, indexUuid=indexUuid, epoch=epoch)
                if claim is None:
                    raise LexicalIndexError("Indexing execution lost admission authority")
                payload: LexicalInput = self._prepareInput(claim)
                self._assertClaim(claim)
                if native.getIndexUuid() != claim.indexUuid:
                    raise LexicalIndexError("Native index changed before writing")
                self._writeBuild(native, claim, payload)
                self._assertClaim(claim)
                if native.getIndexUuid() != claim.indexUuid:
                    raise LexicalIndexError("Native index changed before publication")
                outcome: Literal["completed"] = self._publishVisibleBuild(claim, native)
                isKnown = True
                return outcome
        except Exception as error:
            if not isinstance(context, _Context):
                logger.error("Indexing context could not be read; durable intent retained: job_id={}, error_type={}", jobId, type(error).__name__)
                return "pending"
            isKnown = not isinstance(error, LexicalIndexError) or not error.hasUnknownOutcome
            settlementDeadline = IndexingDeadline(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)
            self._deadline = settlementDeadline
            try:
                with settlementDeadline.enforce():
                    return self._recordFailure(context, claim, error, ownerToken=ownerToken)
            except Exception as settlementError:
                logger.error("Indexing settlement deferred: job_id={}, error_type={}", jobId, type(settlementError).__name__)
                return "pending"
        finally:
            if native is not None:
                native.close()
            if claim is not None:
                try:
                    self._deadline = settlementDeadline or IndexingDeadline(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)
                    with self._deadline.enforce():
                        self._finishUse(claim, isKnown=isKnown)
                except Exception as settlementError:
                    logger.error("Indexing use remains unresolved: build_id={}, error_type={}", claim.buildId, type(settlementError).__name__)

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as db:
            if self._deadline is not None:
                timeout: str = f"{max(1, int(self._deadline.getRemainingSeconds() * 1000))}ms"
                db.execute(select(func.set_config("statement_timeout", timeout, True),
                    func.set_config("lock_timeout", timeout, True)))
            yield db
            # Do not begin a commit after the transaction used up the budget.
            # A commit already sent may still need authoritative settlement.
            if self._deadline is not None:
                self._deadline.getRemainingSeconds()

    @staticmethod
    def _getExecutionTimeout() -> float:
        return (settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS if settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"
            else settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS)

    def _writeBuild(self, native: OpenSearchLexicalIndex, claim: _Claim, payload: LexicalInput) -> None:
        native.writeBuild(buildId=claim.buildId, units=payload.units,
            beforeBatch=lambda: self._assertClaim(claim))

    def _startExecution(self, context: _Context) -> str | None:
        """Consume the attempt before connection or native lifecycle work."""
        with self._transaction() as db:
            self._authorize(db, context)
            control = self.coordination.lockControl(db)
            lock_namespace_generation(db, user_id=context.scope.ownerId, namespace=context.scope.namespace)
            storage: CorpusStorage = CorpusStorage.resolve_namespace(context.scope.namespace)
            document = db.scalars(select(storage.Document).where(
                storage.Document.document_id == context.documentId,
                storage.Document.user_id == context.scope.ownerId).with_for_update()).one()
            job: Job = db.scalars(select(Job).where(Job.job_id == context.jobId).with_for_update()).one()
            attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(
                IndexingAttempt.id == context.attemptId).with_for_update()).one()
            if (job.current_indexing_attempt_id != attempt.id or job.status in ("done", "failed")
                or attempt.state != "pending" or attempt.executionCount != 0):
                return None
            if document.status == "archived" or document.accepted_job_id != job.job_id:
                raise LexicalIndexError("Indexing candidate is archived or superseded")
            if attempt.backendEpoch != control.epoch:
                raise LexicalIndexError("Indexing attempt crossed a backend transition")
            ownerToken: str = str(uuid4())
            now: datetime = self.coordination.getDatabaseTime(db)
            startedAt: datetime = now - timedelta(seconds=self._deadline.getElapsedSeconds() if self._deadline else 0)
            deadlineAt: datetime = startedAt + timedelta(seconds=self._getExecutionTimeout())
            if attempt.origin == "manual" and attempt.admissionDeadlineAt is not None:
                deadlineAt = min(deadlineAt, attempt.admissionDeadlineAt)
            admissionDeadline: datetime = attempt.admissionDeadlineAt or (
                attempt.createdAt + timedelta(seconds=settings.KNOWHERE_INDEXING_UNSTARTED_TIMEOUT_SECONDS))
            if admissionDeadline <= now:
                raise LexicalIndexError("Indexing intent expired before execution")
            if self._deadline is not None:
                self._deadline.limitRemainingSeconds((deadlineAt - now).total_seconds())
            attempt.state = "running"
            attempt.executionCount = 1
            attempt.ownerToken = ownerToken
            attempt.startedAt = startedAt
            attempt.executionDeadlineAt = deadlineAt
            attempt.claimExpiresAt = deadlineAt
            job.status = "running"
            job.indexing_status = "running"
            job.version += 1
            return ownerToken

    def _readContext(self, jobId: str) -> _Context | str:
        with self._transaction() as db:
            job: Job = db.scalars(select(Job).where(Job.job_id == jobId)).one()
            if job.status in ("done", "failed"):
                return job.status
            if job.parsing_status != "completed" or not job.current_indexing_attempt_id:
                return "skipped"
            storage: CorpusStorage = validate_job_corpus(db, job=job, lock_document=False)
            metadata: dict[str, object] = dict(job.job_metadata or {})
            revisionId: str = db.scalars(select(JobResult.id).where(JobResult.job_id == jobId)).one()
            return _Context(jobId=jobId, revisionId=revisionId,
                documentId=str(metadata["document_id"]), attemptId=job.current_indexing_attempt_id,
                actorId=str(job.user_id),
                scope=LexicalScope(ownerId=storage.resolve_owner(str(job.user_id)),
                    namespace=str(metadata.get("namespace") or "default"),
                    corpusKind="demo" if storage.is_demo else "private"))

    def _claim(self, context: _Context, *, ownerToken: str, indexUuid: str, epoch: int) -> _Claim | None:
        with self._transaction() as db:
            self._authorize(db, context)
            control = self.coordination.lockControl(db)
            lock_namespace_generation(db, user_id=context.scope.ownerId, namespace=context.scope.namespace)
            registry = db.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.id == context.scope.fingerprint).with_for_update()).one()
            storage: CorpusStorage = CorpusStorage.resolve_namespace(context.scope.namespace)
            document = db.scalars(select(storage.Document).where(
                storage.Document.document_id == context.documentId,
                storage.Document.user_id == context.scope.ownerId).with_for_update()).one()
            job: Job = db.scalars(select(Job).where(Job.job_id == context.jobId).with_for_update()).one()
            attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(
                IndexingAttempt.id == context.attemptId).with_for_update()).one()
            now: datetime = self.coordination.getDatabaseTime(db)
            if (control.epoch != epoch or attempt.backendEpoch != epoch or registry.state != "open"
                or registry.indexUuid != indexUuid or job.current_indexing_attempt_id != attempt.id
                or job.status in ("done", "failed") or attempt.state != "running"
                or attempt.ownerToken != ownerToken or attempt.executionCount != 1):
                return None
            if document.status == "archived" or document.accepted_job_id != job.job_id:
                raise LexicalIndexError("Indexing candidate is archived or superseded")
            if attempt.claimExpiresAt is None or attempt.claimExpiresAt <= now:
                raise LexicalIndexError("Indexing execution expired before build preparation")
            if attempt.currentBuildId:
                old: LexicalBuild | None = db.get(LexicalBuild, attempt.currentBuildId)
                if old is not None and old.state == "writing":
                    old.state = "failed"
                    old.cleanupState = "pending"
            build: LexicalBuild = LexicalBuild(attemptId=attempt.id,
                namespaceIndexId=context.scope.fingerprint, documentId=context.documentId,
                revisionId=context.revisionId, ownerToken=ownerToken)
            db.add(build)
            db.flush()
            lease: LexicalLease = LexicalLease(namespaceIndexId=context.scope.fingerprint,
                kind="index_use", ownerToken=ownerToken, backendEpoch=epoch,
                buildId=build.id, expiresAt=now + timedelta(seconds=60))
            registry.lastUsedAt = now
            db.add(lease)
            db.flush()
            attempt.state = "running"
            attempt.ownerToken = ownerToken
            attempt.currentBuildId = build.id
            attempt.claimExpiresAt = attempt.executionDeadlineAt
            job.status = "running"
            job.indexing_status = "running"
            job.version += 1
            return _Claim(context=context, buildId=build.id, ownerToken=ownerToken,
                leaseId=lease.id, epoch=epoch, indexUuid=indexUuid)

    def _lockClaim(self, db: Session, claim: _Claim) -> _LockedClaim:
        context: _Context = claim.context
        self._authorize(db, context)
        control = self.coordination.lockControl(db)
        generation: RetrievalNamespaceGeneration = lock_namespace_generation(db,
            user_id=context.scope.ownerId, namespace=context.scope.namespace)
        registry = db.scalars(select(LexicalNamespaceIndex).where(
            LexicalNamespaceIndex.id == context.scope.fingerprint).with_for_update(read=True)).one()
        storage: CorpusStorage = CorpusStorage.resolve_namespace(context.scope.namespace)
        document = db.scalars(select(storage.Document).where(
            storage.Document.document_id == context.documentId,
            storage.Document.user_id == context.scope.ownerId).with_for_update()).one()
        job: Job = db.scalars(select(Job).where(Job.job_id == context.jobId).with_for_update()).one()
        validate_job_corpus(db, job=job, lock_document=False)
        attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(
            IndexingAttempt.id == context.attemptId).with_for_update()).one()
        build: LexicalBuild = db.scalars(select(LexicalBuild).where(
            LexicalBuild.id == claim.buildId).with_for_update()).one()
        lease: LexicalLease = db.scalars(select(LexicalLease).where(
            LexicalLease.id == claim.leaseId).with_for_update()).one()
        now: datetime = self.coordination.getDatabaseTime(db)
        if (control.epoch != claim.epoch or registry.indexUuid != claim.indexUuid
            or build.state != "writing"
            or attempt.ownerToken != claim.ownerToken or attempt.currentBuildId != claim.buildId
            or attempt.state != "running" or attempt.claimExpiresAt is None or attempt.claimExpiresAt <= now
            or lease.state != "active" or lease.expiresAt <= now
            or job.current_indexing_attempt_id != attempt.id or job.status == "done"):
            raise LexicalIndexError("Indexing execution lost its publication authority", isRetryable=True)
        if document.status == "archived" or document.accepted_job_id != job.job_id:
            raise LexicalIndexError("Indexing candidate is archived or superseded")
        return _LockedClaim(job=job, document=document, attempt=attempt, build=build,
            lease=lease, now=now, generation=generation)

    def _authorize(self, db: Session, context: _Context) -> None:
        if context.scope.corpusKind == "demo":
            authorize_demo_transaction(db, user_id=context.actorId)

    def _renew(self, claim: _Claim) -> bool:
        try:
            with self._transaction() as db:
                locked: _LockedClaim = self._lockClaim(db, claim)
                locked.lease.expiresAt = locked.attempt.executionDeadlineAt or locked.now
                locked.job.updated_at = locked.now.replace(tzinfo=None)
            return True
        except Exception:
            return False

    def _assertClaim(self, claim: _Claim) -> None:
        with self._transaction() as db:
            self._lockClaim(db, claim)

    def _prepareInput(self, claim: _Claim) -> LexicalInput:
        source: _InputSource = self._readInputSource(claim)
        inputs: LexicalInputStorage = LexicalInputStorage()
        if source.key:
            if source.checksum is None:
                raise LexicalIndexError("Assigned indexing input has no checksum")
            payload: LexicalInput = inputs.load(key=source.key, checksum=source.checksum)
        else:
            payload = self._compileInput(claim, source)
            key, checksum, byteCount = inputs.saveWithSize(jobId=claim.context.jobId,
                revisionId=claim.context.revisionId, payload=payload)
            self._assignInput(claim, key, checksum, byteCount)
        self._completeInput(claim, source, payload)
        return payload

    def _readInputSource(self, claim: _Claim) -> _InputSource:
        context: _Context = claim.context
        storage: CorpusStorage = CorpusStorage.resolve_namespace(context.scope.namespace)
        with self._transaction() as db:
            self._lockClaim(db, claim)
            result: JobResult = db.scalars(select(JobResult).where(JobResult.id == context.revisionId)
                .with_for_update(of=JobResult)).one()
            LexicalInputRetention.assertRetained(db, result)
            if result.indexing_input_key is not None:
                # Activation still reads section metadata and graph JSON. Bound
                # those required projections while skipping raw/legacy bodies.
                LexicalInputBudget.assertCanPublish(db, storage=storage,
                    documentId=context.documentId, revisionId=context.revisionId)
                identifiers = db.scalars(select(storage.DocumentSection.section_id).where(
                    storage.DocumentSection.document_id == context.documentId,
                    storage.DocumentSection.job_result_id == context.revisionId))
                sections: tuple[SectionRow, ...] = tuple(SectionRow(section_id=identity,
                    parent_section_id=None, section_path="", section_title="", section_level=0,
                    summary="", sort_order=0) for identity in identifiers)
                return _InputSource(key=result.indexing_input_key, checksum=result.indexing_input_checksum,
                    compiler=result.indexing_input_compiler, sections=sections, chunks=())
            LexicalInputBudget.assertCanHydrate(db, storage=storage,
                documentId=context.documentId, revisionId=context.revisionId)
            sections = tuple(SectionRow(
                section_id=section.section_id, parent_section_id=section.parent_section_id,
                section_path=section.section_path, section_title=section.section_title or "",
                section_level=section.section_level, summary=section.summary or "", sort_order=section.sort_order,
            ) for section in db.scalars(select(storage.DocumentSection).where(
                storage.DocumentSection.document_id == context.documentId,
                storage.DocumentSection.job_result_id == context.revisionId,
            ).order_by(storage.DocumentSection.sort_order, storage.DocumentSection.section_id)))
            chunks: tuple[UnitRow, ...] = ()
            if result.indexing_input_key is None:
                chunks = tuple(UnitRow(
                    chunk_id=chunk.chunk_id, section_id=chunk.section_id, chunk_type=chunk.chunk_type,
                    content=chunk.content or "", sort_order=chunk.sort_order,
                    source_chunk_path=chunk.source_chunk_path or "", file_path=chunk.file_path or "",
                    metadata=dict(chunk.chunk_metadata or {}),
                ) for chunk in db.scalars(select(storage.DocumentChunk).where(
                    storage.DocumentChunk.document_id == context.documentId,
                    storage.DocumentChunk.job_result_id == context.revisionId,
                ).order_by(storage.DocumentChunk.sort_order, storage.DocumentChunk.chunk_id, storage.DocumentChunk.id)))
            return _InputSource(key=result.indexing_input_key, checksum=result.indexing_input_checksum,
                compiler=result.indexing_input_compiler, sections=sections, chunks=chunks)

    def _compileInput(self, claim: _Claim, source: _InputSource) -> LexicalInput:
        if source.compiler != LexicalPreparation.getCompilerFingerprint():
            raise LexicalIndexError("Retained parsing input requires its original indexing compiler")
        if self._deadline is not None:
            self._deadline.getRemainingSeconds()
        checkpoint: CompilationCheckpoint = CompilationCheckpoint(self._deadline)
        payload: LexicalInput = LexicalPreparation.prepare(documentId=claim.context.documentId,
            revisionId=claim.context.revisionId, sections=source.sections, chunks=source.chunks, checkpoint=checkpoint.check)
        if self._deadline is not None:
            self._deadline.getRemainingSeconds()
        return payload

    def _assignInput(self, claim: _Claim, key: str, checksum: str, byteCount: int) -> None:
        with self._transaction() as db:
            self._lockClaim(db, claim)
            result: JobResult = db.scalars(select(JobResult).where(
                JobResult.id == claim.context.revisionId).with_for_update(of=JobResult)).one()
            LexicalInputRetention.assertRetained(db, result, key=key)
            if result.indexing_input_key is None:
                result.indexing_input_key = key
                result.indexing_input_checksum = checksum
                result.indexing_input_bytes = byteCount
            elif result.indexing_input_key != key or result.indexing_input_checksum != checksum:
                raise LexicalIndexError("Immutable indexing input assignment changed")

    def _completeInput(self, claim: _Claim, source: _InputSource, payload: LexicalInput) -> None:
        allowedSections: set[str] = {section.section_id for section in source.sections}
        if (any(unit.documentId != claim.context.documentId or unit.revisionId != claim.context.revisionId
                or unit.sectionId not in allowedSections for unit in payload.units)
            or len({unit.unitId for unit in payload.units}) != len(payload.units)):
            raise LexicalIndexError("Retained lexical input escaped its revision binding")
        if source.compiler is not None and payload.compilerFingerprint != source.compiler:
            raise LexicalIndexError("Retained indexing input compiler binding changed")
        with self._transaction() as db:
            locked: _LockedClaim = self._lockClaim(db, claim)
            result: JobResult = db.scalars(select(JobResult).where(JobResult.id == claim.context.revisionId)
                .with_for_update(of=JobResult)).one()
            LexicalInputRetention.assertRetained(db, result)
            self._persistUnitMetadata(db, claim, payload)
            locked.build.unitCount = len(payload.units)

    def _persistUnitMetadata(self, db: Session, claim: _Claim, payload: LexicalInput) -> None:
        context: _Context = claim.context
        storage: CorpusStorage = CorpusStorage.resolve_namespace(context.scope.namespace)
        persist_unit_metadata(db, storage=storage, document_id=context.documentId,
            revision_id=context.revisionId, payload=payload)
        job: Job = db.scalars(select(Job).where(Job.job_id == context.jobId)).one()
        persist_revision_serving_state(db, scope=DocumentPublicationScope(
            user_id=context.scope.ownerId, namespace=context.scope.namespace, document_id=context.documentId,
            job_result_id=context.revisionId, source_file_name=str((job.job_metadata or {}).get("source_file_name") or "")))

    def _publishVisibleBuild(self, claim: _Claim, native: OpenSearchLexicalIndex) -> Literal["completed"]:
        publication = ManifestPublication(self.sessions)
        for _ in range(8):
            prepared = publication.prepare(scope=claim.context.scope, documentId=claim.context.documentId,
                buildId=claim.buildId, native=native)
            try:
                return self._activate(claim) if prepared is None else self._activate(claim, prepared)
            except ManifestPublicationAdvanced:
                continue
        raise ManifestPublicationAdvanced("Namespace manifest kept advancing during publication", isRetryable=True)

    def _activate(self, claim: _Claim, prepared: PreparedManifest | None = None) -> Literal["completed"]:
        eventId: str | None = None
        deferCopy: bool = settings.KNOWHERE_OPENSEARCH_PG_COPY_ON_DEMAND
        with self._transaction() as db:
            locked: _LockedClaim = self._lockClaim(db, claim)
            generationBefore: int = locked.generation.generation
            context: _Context = claim.context
            metadata: dict[str, object] = dict(locked.job.job_metadata or {})
            oldBuildId: str | None = locked.document.current_indexing_build_id
            if prepared is not None and oldBuildId is not None:
                BuildSnapshot.capture(db, scope=context.scope, buildId=oldBuildId)
            locked.document.current_job_result_id = context.revisionId
            locked.document.current_indexing_build_id = claim.buildId
            locked.document.source_file_name = str(metadata.get("source_file_name") or locked.document.source_file_name or "")
            locked.document.parse_track = str(metadata.get("parse_track") or "chunk")
            documentMetadata: object = metadata.get("document_metadata")
            if isinstance(documentMetadata, dict) and documentMetadata:
                locked.document.document_metadata = dict(documentMetadata)
            scope: DocumentPublicationScope = DocumentPublicationScope(user_id=context.scope.ownerId,
                namespace=context.scope.namespace, document_id=context.documentId,
                job_result_id=context.revisionId, source_file_name=locked.document.source_file_name)
            publication: RetrievalPublicationService = RetrievalPublicationService()
            publication.publish_document_graph(db, job_id=context.jobId, job_result_id=context.revisionId,
                top_summary=str(metadata.get("document_top_summary") or ""))
            manifest = persist_revision_serving_state(db, scope=scope)
            publication.update_namespace_snapshot(db, scope=scope, manifest_payload=manifest,
                locked_generation=locked.generation)
            generation: RetrievalNamespaceGeneration = locked.generation
            if oldBuildId and oldBuildId != claim.buildId:
                old: LexicalBuild | None = db.get(LexicalBuild, oldBuildId)
                if old is not None:
                    old.state = "retired"
                    old.retirementGeneration = generation.generation
                    old.cleanupState = "pending"
                    old.retainForManifestReads = prepared is not None
            locked.build.state = "published"
            if prepared is not None:
                BuildSnapshot.capture(db, scope=context.scope, buildId=claim.buildId)
            ManifestPublication.activate(db, prepared, previousGeneration=generationBefore,
                publishedGeneration=generation.generation)
            locked.build.activationGeneration = generation.generation
            locked.build.legacyCopyState = "skipped" if deferCopy else "pending"
            locked.attempt.state = "succeeded"
            locked.attempt.finishedAt = locked.now
            locked.attempt.claimExpiresAt = None
            locked.job.indexing_status = "completed"
            locked.job.failure_stage = None
            db.flush()
            if not SyncStateMachineService().mark_completed_outcome(db, context.jobId).succeeded:
                raise LexicalIndexError("Job completion rejected after visible indexing")
            event = SyncJobWebhookOutbox().create_event(db, job_id=context.jobId, event_type="job.completed")
            eventId = event.event_id if event else None
        SyncJobWebhookOutbox().enqueue_event_id_after_commit(eventId)
        if not deferCopy:
            try:
                from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
                LegacyCopyDispatch.submit(claim.buildId)
            except Exception as error:
                logger.error("PG rollback copy submission deferred: build_id={}, error_type={}", claim.buildId, type(error).__name__)
        return "completed"

    def _recordFailure(self, context: _Context, claim: _Claim | None, error: Exception,
                       *, ownerToken: str | None = None) -> Literal["completed", "pending", "failed", "skipped"]:
        eventId: str | None = None
        with self._transaction() as db:
            self._authorize(db, context)
            job: Job = db.scalars(select(Job).where(Job.job_id == context.jobId).with_for_update()).one()
            if job.status == "done":
                return "completed"
            attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(
                IndexingAttempt.id == context.attemptId).with_for_update()).one()
            if job.current_indexing_attempt_id != attempt.id:
                return "skipped"
            expectedOwner: str | None = claim.ownerToken if claim is not None else ownerToken
            if attempt.state == "running" and attempt.ownerToken != expectedOwner:
                return "pending"
            code: str = error.getIndexingFailureCode() if isinstance(error, LexicalIndexError) else "UNAVAILABLE"
            eventId = IndexingSettlement.fail(db, job, attempt,
                message=str(error) if isinstance(error, LexicalIndexError) else type(error).__name__, code=code)
        logger.error("Indexing failed: job_id={}, attempt_id={}, error_type={}", context.jobId, context.attemptId, type(error).__name__)
        SyncJobWebhookOutbox().enqueue_event_id_after_commit(eventId)
        return "failed"

    def _finishUse(self, claim: _Claim, *, isKnown: bool) -> None:
        with self._transaction() as db:
            lease: LexicalLease | None = db.get(LexicalLease, claim.leaseId)
            if lease is not None and lease.ownerToken == claim.ownerToken:
                lease.state = "released" if isKnown else "uncertain"
