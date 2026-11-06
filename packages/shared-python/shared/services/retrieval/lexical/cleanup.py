"""Sweep exact retired builds without waiting for readers; track late writes."""

from datetime import timedelta
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


class LexicalCleanup:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()

    def closeEmptyNamespaces(self) -> int:
        with self.sessions.begin() as db:
            now = LexicalCoordination.getDatabaseTime(db)
            rows: list[LexicalNamespaceIndex] = list(db.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.state == "open",
                or_(LexicalNamespaceIndex.nextMaintenanceAt.is_(None), LexicalNamespaceIndex.nextMaintenanceAt <= now),
            ).order_by(LexicalNamespaceIndex.nextMaintenanceAt.asc().nullsfirst(), LexicalNamespaceIndex.id)
                .limit(20).with_for_update(skip_locked=True)))
            identities: tuple[tuple[str, str, str, str], ...] = tuple((row.id, row.ownerId, row.namespace, row.corpusKind) for row in rows)
            for row in rows:
                row.nextMaintenanceAt = now + timedelta(minutes=5)
            retained: int = int(db.scalar(select(func.count()).select_from(LexicalNamespaceIndex)) or 0)
        completed: int = 0
        for identity, ownerId, namespace, corpusKind in identities:
            native: OpenSearchLexicalIndex | None = None
            errorType: str | None = None
            try:
                if corpusKind not in ("private", "demo"):
                    raise LexicalIndexError("Empty namespace has an invalid corpus identity")
                scope: LexicalScope = LexicalScope(ownerId=ownerId, namespace=namespace,
                    corpusKind="demo" if corpusKind == "demo" else "private")
                if identity != scope.fingerprint:
                    raise LexicalIndexError("Empty namespace escaped its registered identity")
                native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                    scope=scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
                completed += int(LexicalCoordination(self.sessions).closeEmpty(scope=scope, native=native))
            except Exception as error:
                errorType = type(error).__name__
                logger.warning("Empty namespace close deferred: registry_id={} error_type={}", identity, errorType)
            finally:
                if native is not None:
                    native.close()
                with self.sessions.begin() as db:
                    row = db.get(LexicalNamespaceIndex, identity, with_for_update=True)
                    if row is not None:
                        row.maintenanceError = errorType
        logger.info("Lexical retained catalog: indexes={} close_scan_limit=20 closed_empty={}", retained, completed)
        return completed

    def collect(self, buildId: str) -> bool:
        owner: str = str(uuid4())
        with self.sessions.begin() as db:
            source: LexicalBuild | None = db.get(LexicalBuild, buildId)
            if source is None:
                return True
            registry: LexicalNamespaceIndex = db.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.id == source.namespaceIndexId)).one()
            if registry.corpusKind not in ("private", "demo"):
                raise LexicalIndexError("Cleanup encountered an invalid corpus identity")
            scope: LexicalScope = LexicalScope(ownerId=registry.ownerId,
                namespace=registry.namespace, corpusKind="demo" if registry.corpusKind == "demo" else "private")
            actorId: str = db.scalars(select(Job.user_id).join(IndexingAttempt,
                IndexingAttempt.jobId == Job.job_id).where(IndexingAttempt.id == source.attemptId)).one()
            needsOpening: bool = registry.state == "closed" and source.state in ("retired", "failed")
        if needsOpening:
            recoveryNative: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
                endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
                indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
            try:
                LexicalCoordination(self.sessions).ensureIndex(scope=scope, native=recoveryNative)
            finally:
                recoveryNative.close()
        with self.sessions.begin() as db:
            if scope.corpusKind == "demo":
                authorize_demo_transaction(db, user_id=actorId)
            control = LexicalCoordination.lockControl(db)
            registry = db.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.id == scope.fingerprint)).one()
            storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
            document = db.scalar(select(storage.Document).where(
                storage.Document.document_id == source.documentId,
                storage.Document.user_id == scope.ownerId).with_for_update())
            build: LexicalBuild = db.scalars(select(LexicalBuild).where(
                LexicalBuild.id == buildId).with_for_update()).one()
            if build.state not in ("retired", "failed") or (
                document is not None and document.current_indexing_build_id == buildId):
                return False
            now = LexicalCoordination.getDatabaseTime(db)
            if registry.state != "open" or registry.indexUuid is None:
                return False
            indexUuid: str = registry.indexUuid
            epoch: int = control.epoch
            use: LexicalLease = LexicalLease(namespaceIndexId=scope.fingerprint,
                kind="index_use", ownerToken=owner, backendEpoch=epoch,
                expiresAt=now + timedelta(seconds=60))
            db.add(use)
            db.flush()
            leaseId: str = use.id
        native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
            endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
        isKnown: bool = True
        isEmpty: bool = False
        try:
            if native.getClusterUuid() != self._getClusterUuid() or native.getIndexUuid() != indexUuid:
                raise LexicalIndexError("Cleanup encountered a changed native identity")
            isEmpty = native.deleteBuild(buildId)
        except LexicalIndexError as error:
            isKnown = not error.hasUnknownOutcome
            raise
        finally:
            native.close()
            with self.sessions.begin() as db:
                use = db.scalars(select(LexicalLease).where(LexicalLease.id == leaseId).with_for_update()).one()
                use.state = "released" if isKnown else "uncertain"
        with self.sessions.begin() as db:
            if scope.corpusKind == "demo":
                authorize_demo_transaction(db, user_id=actorId)
            control = LexicalCoordination.lockControl(db)
            build = db.scalars(select(LexicalBuild).where(LexicalBuild.id == buildId).with_for_update()).one()
            hasLateWriter: bool = bool(db.scalar(select(func.count()).select_from(LexicalLease).where(
                LexicalLease.buildId == buildId, LexicalLease.kind == "index_use",
                LexicalLease.state != "released")))
            isComplete: bool = isEmpty and not hasLateWriter and control.epoch == epoch
            build.cleanupState = "completed" if isComplete else "pending"
            return isComplete

    def _getClusterUuid(self) -> str | None:
        with self.sessions.begin() as db:
            return LexicalCoordination.lockControl(db).clusterUuid
