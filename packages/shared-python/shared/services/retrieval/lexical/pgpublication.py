"""Publish frozen PG lexical rows before a short, owner-fenced activation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.document import Document
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.compilation_checkpoint import CompilationCheckpoint
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.input_budget import LexicalInputBudget
from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.publication_token_copy import (
    insert_token_records_with_copy,
)
from shared.services.retrieval.scoring.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
)
from shared.services.retrieval.scoring.knowhere_provider import SectionRow, UnitRow
from shared.services.retrieval.scoring.persisted_score_load import (
    average_idf_from_unit_dfs,
)
from shared.services.retrieval.serving_generation import lock_namespace_generation
from shared.services.retrieval.serving_manifest import persist_revision_serving_state

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.worker import _Context


@dataclass(frozen=True)
class _PublicationResult:
    eventId: str | None


@dataclass(frozen=True)
class _InputState:
    key: str | None
    checksum: str | None
    compiler: str | None
    sections: tuple[SectionRow, ...] = ()
    chunks: tuple[UnitRow, ...] = ()


class PostgresqlPublication:
    """The caller claims one execution and owns failure/outbox dispatch policy.

    Only the immutable revision is written during COPY. Control, document and
    namespace publication locks are held only in short admission/activation
    transactions. The async entry uses the original request's async SQL pool;
    every SQL operation creates its own Session and object I/O remains async.
    """

    def __init__(
        self, *, sessionFactory: sessionmaker[Session], deadline: IndexingDeadline
    ) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory
        self.deadline: IndexingDeadline = deadline

    def run(self, context: _Context, *, ownerToken: str) -> _PublicationResult:
        state: _InputState = self._readInput(context, ownerToken)
        inputs: LexicalInputStorage = LexicalInputStorage()
        if state.key:
            payload: LexicalInput = inputs.load(
                key=state.key, checksum=self._requireChecksum(state)
            )
        else:
            payload = self._prepareInput(context, state)
            key, checksum, size = inputs.saveWithSize(
                jobId=context.jobId, revisionId=context.revisionId, payload=payload
            )
            self._assignInput(context, ownerToken, key, checksum, size)
        self.deadline.getRemainingSeconds()
        self._validatePayload(context, state, payload)
        self._writePrepared(context, ownerToken, payload)
        return self._activate(context, ownerToken, payload)

    async def runAsync(
        self, context: _Context, *, ownerToken: str, database: AsyncLexicalDatabase
    ) -> _PublicationResult:
        state: _InputState = await database.run(
            lambda: self._readInput(context, ownerToken)
        )
        inputs: LexicalInputStorage = LexicalInputStorage()
        if state.key:
            payload: LexicalInput = await inputs.loadAsync(
                key=state.key,
                checksum=self._requireChecksum(state),
                deadline=self.deadline,
            )
        else:
            if state.compiler != LexicalPreparation.getCompilerFingerprint():
                raise LexicalIndexError(
                    "Retained parsing input requires its original indexing compiler"
                )
            payload = await LexicalPreparation.prepareAsync(
                documentId=context.documentId,
                revisionId=context.revisionId,
                sections=state.sections,
                chunks=state.chunks,
                deadline=self.deadline,
            )
            key, checksum, size = await inputs.saveAsync(
                jobId=context.jobId,
                revisionId=context.revisionId,
                payload=payload,
                deadline=self.deadline,
            )
            await database.run(
                lambda: self._assignInput(context, ownerToken, key, checksum, size)
            )
        self.deadline.getRemainingSeconds()
        self._validatePayload(context, state, payload)
        await database.run(lambda: self._writePrepared(context, ownerToken, payload))
        return await database.run(lambda: self._activate(context, ownerToken, payload))

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as db:
            timeout: str = (
                f"{max(1, int(self.deadline.getRemainingSeconds() * 1000))}ms"
            )
            db.execute(
                select(
                    func.set_config("statement_timeout", timeout, True),
                    func.set_config("lock_timeout", timeout, True),
                )
            )
            yield db
            self.deadline.getRemainingSeconds()

    def _checkOwnership(
        self, db: Session, context: _Context, ownerToken: str, *, lock: bool
    ) -> tuple[Job, Document | DemoDocument, IndexingAttempt]:
        control: LexicalBackendControl | None = (
            LexicalCoordination.lockControl(db)
            if lock
            else db.scalar(
                select(LexicalBackendControl).where(LexicalBackendControl.id == 1)
            )
        )
        storage: CorpusStorage = CorpusStorage.resolve_namespace(
            context.scope.namespace
        )
        documentStatement = select(storage.Document).where(
            storage.Document.document_id == context.documentId,
            storage.Document.user_id == context.scope.ownerId,
            storage.Document.namespace == context.scope.namespace,
        )
        jobStatement = select(Job).where(Job.job_id == context.jobId)
        attemptStatement = select(IndexingAttempt).where(
            IndexingAttempt.id == context.attemptId
        )
        if lock:
            documentStatement = documentStatement.with_for_update()
            jobStatement = jobStatement.with_for_update()
            attemptStatement = attemptStatement.with_for_update()
        document = db.scalars(documentStatement).one()
        job: Job = db.scalars(jobStatement).one()
        attempt: IndexingAttempt = db.scalars(attemptStatement).one()
        validate_job_corpus(db, job=job, lock_document=False)
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        if (
            control is None
            or control.backend != "postgresql"
            or control.mode != "serving"
            or control.epoch != attempt.backendEpoch
            or job.current_indexing_attempt_id != attempt.id
            or job.status in ("done", "failed")
            or job.parsing_status != "completed"
            or attempt.jobId != context.jobId
            or attempt.revisionId != context.revisionId
            or attempt.state != "running"
            or attempt.executionCount != 1
            or attempt.ownerToken != ownerToken
            or attempt.executionDeadlineAt is None
            or attempt.executionDeadlineAt <= now
            or attempt.claimExpiresAt is None
            or attempt.claimExpiresAt <= now
            or document.status != "active"
            or document.accepted_job_id != context.jobId
            or storage.resolve_owner(str(job.user_id)) != context.scope.ownerId
        ):
            raise LexicalIndexError(
                "PG publication lost its backend, revision or execution authority",
                isRetryable=True,
            )
        return job, document, attempt

    def _readInput(self, context: _Context, ownerToken: str) -> _InputState:
        with self._transaction() as db:
            self._checkOwnership(db, context, ownerToken, lock=True)
            result: JobResult = db.scalars(
                select(JobResult)
                .where(
                    JobResult.id == context.revisionId,
                    JobResult.job_id == context.jobId,
                )
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result)
            if result.indexing_input_key:
                return _InputState(
                    result.indexing_input_key,
                    result.indexing_input_checksum,
                    result.indexing_input_compiler,
                )
            storage: CorpusStorage = CorpusStorage.resolve_namespace(
                context.scope.namespace
            )
            LexicalInputBudget.assertCanHydrate(
                db,
                storage=storage,
                documentId=context.documentId,
                revisionId=context.revisionId,
            )
            sections: tuple[SectionRow, ...] = tuple(
                SectionRow(
                    section_id=row.section_id,
                    section_path=row.section_path,
                    parent_section_id=row.parent_section_id,
                    section_title=row.section_title or "",
                    summary=row.summary or "",
                    sort_order=row.sort_order,
                    section_level=row.section_level,
                )
                for row in db.scalars(
                    select(storage.DocumentSection)
                    .where(
                        storage.DocumentSection.document_id == context.documentId,
                        storage.DocumentSection.job_result_id == context.revisionId,
                    )
                    .order_by(
                        storage.DocumentSection.sort_order,
                        storage.DocumentSection.section_id,
                    )
                )
            )
            chunks: tuple[UnitRow, ...] = tuple(
                UnitRow(
                    chunk_id=row.chunk_id,
                    section_id=row.section_id,
                    chunk_type=row.chunk_type,
                    content=row.content or "",
                    sort_order=row.sort_order,
                    source_chunk_path=row.source_chunk_path or "",
                    file_path=row.file_path or "",
                    metadata=dict(row.chunk_metadata or {}),
                )
                for row in db.scalars(
                    select(storage.DocumentChunk)
                    .where(
                        storage.DocumentChunk.document_id == context.documentId,
                        storage.DocumentChunk.job_result_id == context.revisionId,
                    )
                    .order_by(
                        storage.DocumentChunk.sort_order,
                        storage.DocumentChunk.chunk_id,
                        storage.DocumentChunk.id,
                    )
                )
            )
            return _InputState(
                None, None, result.indexing_input_compiler, sections, chunks
            )

    @staticmethod
    def _requireChecksum(state: _InputState) -> str:
        if not state.checksum:
            raise LexicalIndexError("Assigned PG indexing input has no checksum")
        return state.checksum

    def _prepareInput(self, context: _Context, state: _InputState) -> LexicalInput:
        if state.compiler != LexicalPreparation.getCompilerFingerprint():
            raise LexicalIndexError(
                "Retained parsing input requires its original indexing compiler"
            )

        checkpoint: CompilationCheckpoint = CompilationCheckpoint(self.deadline)
        return LexicalPreparation.prepare(
            documentId=context.documentId,
            revisionId=context.revisionId,
            sections=state.sections,
            chunks=state.chunks,
            checkpoint=checkpoint.check,
        )

    @staticmethod
    def _validatePayload(
        context: _Context, state: _InputState, payload: LexicalInput
    ) -> None:
        if payload.compilerFingerprint != state.compiler or len(
            {unit.unitId for unit in payload.units}
        ) != len(payload.units):
            raise LexicalIndexError(
                "Frozen PG input compiler or unit identities are inconsistent"
            )
        if any(
            unit.documentId != context.documentId
            or unit.revisionId != context.revisionId
            for unit in payload.units
        ):
            raise LexicalIndexError("Frozen PG input escaped its document revision")

    def _assignInput(
        self, context: _Context, ownerToken: str, key: str, checksum: str, size: int
    ) -> None:
        with self._transaction() as db:
            self._checkOwnership(db, context, ownerToken, lock=True)
            result: JobResult = db.scalars(
                select(JobResult)
                .where(JobResult.id == context.revisionId)
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result, key=key)
            if result.indexing_input_key not in (None, key):
                raise LexicalIndexError(
                    "Frozen PG indexing input changed during preparation"
                )
            result.indexing_input_key = key
            result.indexing_input_checksum = checksum
            result.indexing_input_bytes = size

    def _writePrepared(
        self, context: _Context, ownerToken: str, payload: LexicalInput
    ) -> None:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(
            context.scope.namespace
        )
        with self._transaction() as db:
            # MVCC reads validate admission without retaining publication locks
            # through the potentially long COPY. Activation checks again below.
            self._checkOwnership(db, context, ownerToken, lock=False)
            self.writeFrozenRevision(
                db,
                documentId=context.documentId,
                revisionId=context.revisionId,
                storage=storage,
                payload=payload,
            )

    def writeFrozenRevision(
        self,
        db: Session,
        *,
        documentId: str,
        revisionId: str,
        storage: CorpusStorage,
        payload: LexicalInput,
    ) -> None:
        """Write one derived revision; caller owns admission and short activation guards.

        Used by an already-claimed PG execution or explicitly authorized repair
        of the current published revision. It never changes published pointers.
        """
        if any(
            unit.documentId != documentId or unit.revisionId != revisionId
            for unit in payload.units
        ):
            raise LexicalIndexError("Frozen PG write escaped its document revision")
        identity: int = int.from_bytes(
            sha256(f"legacy-copy:{revisionId}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        db.execute(select(func.pg_advisory_xact_lock(identity)))
        db.execute(
            delete(storage.DocumentMapUnitIndex).where(
                storage.DocumentMapUnitIndex.document_id == documentId,
                storage.DocumentMapUnitIndex.job_result_id == revisionId,
            )
        )
        db.execute(
            delete(storage.DocumentMapUnit).where(
                storage.DocumentMapUnit.document_id == documentId,
                storage.DocumentMapUnit.job_result_id == revisionId,
            )
        )
        unitIds: dict[str, str] = {}
        metadata: list[dict[str, object]] = []
        for unit in payload.units:
            unitId: str = (
                "dmu_"
                + sha256(json.dumps([revisionId, unit.unitId]).encode()).hexdigest()
            )
            unitIds[unit.unitId] = unitId
            metadata.append(
                {
                    "id": unitId,
                    "document_id": documentId,
                    "job_result_id": revisionId,
                    "unit_id": unit.unitId,
                    "section_id": unit.sectionId,
                    "unit_kind": "self_only"
                    if unit.unitId.endswith("__self")
                    else "leaf",
                    "path_token_count": len(unit.pathTokens.split()),
                    "content_token_count": len(unit.contentTokens.split()),
                    "has_image": unit.hasImage,
                    "has_table": unit.hasTable,
                    "sort_order": unit.sortOrder,
                }
            )
        for offset in range(0, len(metadata), 1000):
            db.execute(
                insert(storage.DocumentMapUnit).values(metadata[offset : offset + 1000])
            )
        rows, pathFrequency, contentFrequency = LegacyLexicalCopy._prepareRows(
            payload, unitIds, revisionId
        )
        insert_token_records_with_copy(db, rows, corpus_storage=storage)
        self.deadline.getRemainingSeconds()
        db.add(
            storage.DocumentMapUnitIndex(
                id=f"dmui_{uuid4().hex}",
                document_id=documentId,
                job_result_id=revisionId,
                format_version=MAP_UNIT_INDEX_FORMAT_VERSION,
                unit_count=len(payload.units),
                token_count=len(rows),
                average_idf_path=average_idf_from_unit_dfs(
                    unit_count=len(unitIds), token_document_frequency=pathFrequency
                ),
                average_idf_content=average_idf_from_unit_dfs(
                    unit_count=len(unitIds), token_document_frequency=contentFrequency
                ),
                path_document_count=sum(
                    bool(unit.pathTokens) for unit in payload.units
                ),
                content_document_count=sum(
                    bool(unit.contentTokens) for unit in payload.units
                ),
                path_total_length=sum(
                    len(unit.pathTokens.split()) for unit in payload.units
                ),
                content_total_length=sum(
                    len(unit.contentTokens.split()) for unit in payload.units
                ),
            )
        )

    def _activate(
        self, context: _Context, ownerToken: str, payload: LexicalInput
    ) -> _PublicationResult:
        with self._transaction() as db:
            LexicalCoordination.lockControl(db)
            generation = lock_namespace_generation(
                db, user_id=context.scope.ownerId, namespace=context.scope.namespace
            )
            job, document, attempt = self._checkOwnership(
                db, context, ownerToken, lock=True
            )
            storage: CorpusStorage = CorpusStorage.resolve_namespace(
                context.scope.namespace
            )
            LexicalInputBudget.assertCanPublish(
                db,
                storage=storage,
                documentId=context.documentId,
                revisionId=context.revisionId,
            )
            marker = db.scalars(
                select(storage.DocumentMapUnitIndex).where(
                    storage.DocumentMapUnitIndex.document_id == context.documentId,
                    storage.DocumentMapUnitIndex.job_result_id == context.revisionId,
                )
            ).one()
            if marker.unit_count != len(payload.units):
                raise LexicalIndexError(
                    "PG publication index coverage changed before activation"
                )
            result: JobResult = db.scalars(
                select(JobResult)
                .where(JobResult.id == context.revisionId)
                .with_for_update(of=JobResult)
            ).one()
            LexicalInputRetention.assertRetained(db, result)
            oldBuildId: str | None = document.current_indexing_build_id
            document.current_job_result_id = context.revisionId
            document.current_indexing_build_id = None
            metadata: dict[str, object] = dict(job.job_metadata or {})
            document.source_file_name = str(
                metadata.get("source_file_name") or document.source_file_name or ""
            )
            document.parse_track = str(metadata.get("parse_track") or "chunk")
            documentMetadata: object = metadata.get("document_metadata")
            if isinstance(documentMetadata, dict) and documentMetadata:
                document.document_metadata = dict(documentMetadata)
            scope: DocumentPublicationScope = DocumentPublicationScope(
                user_id=context.scope.ownerId,
                namespace=context.scope.namespace,
                document_id=context.documentId,
                job_result_id=context.revisionId,
                source_file_name=document.source_file_name,
            )
            publication: RetrievalPublicationService = RetrievalPublicationService()
            publication.publish_document_graph(
                db,
                job_id=context.jobId,
                job_result_id=context.revisionId,
                top_summary=str(metadata.get("document_top_summary") or ""),
            )
            manifest = persist_revision_serving_state(db, scope=scope)
            publication.update_namespace_snapshot(
                db, scope=scope, manifest_payload=manifest, locked_generation=generation
            )
            if oldBuildId:
                old: LexicalBuild | None = db.get(LexicalBuild, oldBuildId)
                if old is not None:
                    old.state = "retired"
                    old.retirementGeneration = generation.generation
                    old.cleanupState = "pending"
            attempt.state = "succeeded"
            attempt.finishedAt = LexicalCoordination.getDatabaseTime(db)
            attempt.claimExpiresAt = None
            job.indexing_status = "completed"
            job.failure_stage = None
            db.flush()
            if (
                not SyncStateMachineService()
                .mark_completed_outcome(db, context.jobId)
                .succeeded
            ):
                raise LexicalIndexError(
                    "PG job completion rejected after index preparation"
                )
            event = SyncJobWebhookOutbox().create_event(
                db, job_id=context.jobId, event_type="job.completed"
            )
            return _PublicationResult(event.event_id if event else None)
