"""Capture immutable published bindings without registering or locking readers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from types import MappingProxyType
import time

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.job_result import JobResult
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.recency import LexicalRecency
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.stage_observer import StageObserver

_ACTIVE_READ: ContextVar[PublishedLexicalRead | None] = ContextVar("lexical_read_session", default=None)


class PublishedLexicalRead:
    def __init__(self, *, userId: str, namespace: str,
                 sessionFactory: sessionmaker[Session] | None = None,
                 isClassic: bool = False) -> None:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        self.scope: LexicalScope = LexicalScope(ownerId=storage.resolve_owner(userId),
            namespace=namespace, corpusKind="demo" if storage.is_demo else "private")
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()
        self.coordination: LexicalCoordination = LexicalCoordination(self.sessions)
        self.pins: RetrievalRevisionPins = RetrievalRevisionPins({})
        self.builds: Mapping[str, str] = MappingProxyType({})
        self.epoch: int = 0
        self.isClassic: bool = isClassic
        self.progressiveRevisions: Mapping[str, str] = MappingProxyType({})
        self._indexUuid: str | None = None
        self._native: OpenSearchLexicalIndex | None = None
        self._hasStarted: bool = False
        self._isFinished: bool = False

    @staticmethod
    def current() -> PublishedLexicalRead | None:
        return _ACTIVE_READ.get()

    @property
    def isFinished(self) -> bool:
        return self._isFinished

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = _ACTIVE_READ.set(self)
        try:
            yield
        finally:
            _ACTIVE_READ.reset(token)

    def start(self) -> None:
        self._captureRead()

    def _captureRead(self) -> None:
        if self._hasStarted or self._isFinished:
            raise LexicalIndexError("Retrieval read session is not active", isRetryable=True)
        storage: CorpusStorage = CorpusStorage.resolve_namespace(self.scope.namespace)
        generation = select(storage.RetrievalNamespaceGeneration.generation).where(
            storage.RetrievalNamespaceGeneration.user_id == self.scope.ownerId,
            storage.RetrievalNamespaceGeneration.namespace == self.scope.namespace,
        ).scalar_subquery()
        # One statement binds the generation and all publication pointers even
        # in Read Committed. A one-row anchor preserves the generation of an
        # empty scope without initializing coordination rows from the read path.
        with self.sessions.begin() as db:
            rows: Sequence[tuple[LexicalBackendControl | None, int, str | None, str | None,
                                 str | None, str | None, LexicalBuild | None, bool]] = db.execute(select(
                LexicalBackendControl, func.coalesce(generation, 0),
                LexicalNamespaceIndex.indexName, LexicalNamespaceIndex.indexUuid,
                storage.Document.document_id, storage.Document.current_job_result_id, LexicalBuild,
                func.coalesce(JobResult.document_metadata["ingestion_mode"].as_string() == "progressive", False),
            ).select_from(select(literal(1)).subquery()).outerjoin(LexicalBackendControl,
                LexicalBackendControl.id == 1).outerjoin(LexicalNamespaceIndex,
                LexicalNamespaceIndex.id == self.scope.fingerprint).outerjoin(storage.Document, and_(
                storage.Document.user_id == self.scope.ownerId,
                storage.Document.namespace == self.scope.namespace,
                storage.Document.status == "active",
                storage.Document.current_job_result_id.is_not(None),
            )).outerjoin(LexicalBuild, LexicalBuild.id == storage.Document.current_indexing_build_id)
                .outerjoin(JobResult.__table__, JobResult.id == storage.Document.current_job_result_id)
                .order_by(storage.Document.document_id)).tuples().all()
            control, capturedGeneration, indexName, self._indexUuid, _, _, _, _ = rows[0]
            if control is None and any(row[4] is not None for row in rows):
                raise LexicalIndexError("Lexical backend has not been initialized", isRetryable=True)
            if control is not None and (control.backend != settings.KNOWHERE_LEXICAL_BACKEND or control.mode != "serving"):
                raise LexicalIndexError("Lexical backend transition is in progress", isRetryable=True)
            self.epoch = control.epoch if control is not None else 0
            revisions: dict[str, str] = {}
            builds: dict[str, str] = {}
            progressiveRevisions: dict[str, str] = {}
            expectedName: str = f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{self.scope.fingerprint[:48]}"
            for _, _, _, _, documentId, revisionId, build, hasProgressiveContent in rows:
                if documentId is None:
                    continue
                if (build is None or build.state != "published" or build.documentId != documentId
                    or build.revisionId != revisionId or build.namespaceIndexId != self.scope.fingerprint):
                    raise LexicalIndexError("Published revision has no matching native build")
                if indexName != expectedName or self._indexUuid is None:
                    raise LexicalIndexError("Published revision has no matching native index")
                revisions[documentId] = build.revisionId
                builds[documentId] = build.id
                if hasProgressiveContent:
                    progressiveRevisions[documentId] = build.revisionId
            self.pins = RetrievalRevisionPins(revisions, generation=capturedGeneration)
            self.builds = MappingProxyType(builds)
            self.progressiveRevisions = MappingProxyType(progressiveRevisions)
        self._hasStarted = True

    async def isProgressivePublicationStable(self, db: AsyncSession, scope: DocumentScope) -> bool:
        """Detect a new build of the same captured content, including retired builds."""
        revisions: tuple[str, ...] = tuple(
            revision for identity, revision in self.progressiveRevisions.items() if scope.allows(identity)
        )
        if not revisions:
            return True
        changedBuild: str | None = await db.scalar(select(LexicalBuild.id).where(
            LexicalBuild.namespaceIndexId == self.scope.fingerprint,
            LexicalBuild.revisionId.in_(revisions),
            LexicalBuild.activationGeneration > (self.pins.generation or 0),
        ).limit(1))
        return changedBuild is None

    def assertAlive(self) -> None:
        if not self._hasStarted or self._isFinished:
            raise LexicalIndexError("Retrieval read session is not active", isRetryable=True)

    def search(self, *, buildIds: tuple[str, ...], queryTokens: tuple[str, ...],
               limit: int, unitIds: tuple[str, ...] | None = None,
               sectionIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        self.assertAlive()
        if not buildIds or not queryTokens or unitIds == () or sectionIds == ():
            return ()
        observationStart: float = time.perf_counter()
        StageObserver.add("lexical_executions")
        isCold: bool = False
        LexicalRecency.recordAccess(self.scope.fingerprint)
        if self._native is None:
            self._native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                scope=self.scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
        search = (self._native.search if sectionIds is None
                  else partial(self._native.search, sectionIds=sectionIds))
        try:
            hits: tuple[LexicalHit, ...] = search(buildIds=buildIds,
                queryTokens=queryTokens, limit=limit, unitIds=unitIds)
        except LexicalIndexError as error:
            if not error.isIndexClosed:
                raise
            isCold = True
            indexUuid, epoch = self.coordination.ensureIndex(scope=self.scope, native=self._native)
            self._validateRecovery(indexUuid, epoch)
            hits = search(buildIds=buildIds, queryTokens=queryTokens, limit=limit, unitIds=unitIds)
        self._validateHits(hits)
        elapsed: float = (time.perf_counter() - observationStart) * 1000
        StageObserver.add("lexical_ms", elapsed)
        StageObserver.add("wait_to_discovery_ms", elapsed)
        if isCold:
            StageObserver.add("cold_first_ms", elapsed)
        return hits

    def _validateRecovery(self, indexUuid: str, epoch: int) -> None:
        if epoch != self.epoch or indexUuid != self._indexUuid:
            raise LexicalIndexError("Native read identity changed", isRetryable=True)

    def _validateHits(self, hits: tuple[LexicalHit, ...]) -> None:
        for hit in hits:
            if (self.pins.get(hit.unit.documentId) != hit.unit.revisionId
                or self.builds.get(hit.unit.documentId) != hit.buildId):
                raise LexicalIndexError("Native hit escaped its captured revision scope")

    def finish(self, db: Session | None = None) -> None:
        self.close()

    def close(self) -> None:
        self._isFinished = True
        if self._native is not None:
            self._native.close()
