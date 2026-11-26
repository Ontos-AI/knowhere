"""Remove only records whose document or attempt no longer serves."""

from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.document import Document
from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.lexical.cleanupbatch import CleanupBatch
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


# A fenced writer can have one wait_for bulk request in flight. Let it settle
# before the durable cleanup pass, including after a fast local failure.
NATIVE_WRITE_SETTLEMENT = timedelta(seconds=120)


class AdditiveCleanup:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def run(self) -> int:
        return self.runBatch().succeeded

    def runBatch(self) -> CleanupBatch:
        return self._collectDocuments() + self._collectAttempts()

    def collectDocument(self, documentId: str) -> bool:
        """Clean one due archived document; False leaves it for a later pass."""
        return self._collectDocuments(documentId=documentId).succeeded == 1

    def collectAttempt(self, attemptId: str) -> bool:
        """Clean one due failed enrichment attempt; False leaves it for a later pass."""
        return self._collectAttempts(attemptId=attemptId).succeeded == 1

    def _collectDocuments(self, *, documentId: str | None = None) -> CleanupBatch:
        with self.sessions.begin() as db:
            now = LexicalCoordination.getDatabaseTime(db)
            query = select(Document).where(
                Document.status == "archived", Document.index_cleanup_after <= now,
            )
            if documentId is not None:
                query = query.where(Document.document_id == documentId)
            pending = list(db.scalars(query.order_by(Document.index_cleanup_after, Document.document_id)
                .limit(20).with_for_update(skip_locked=True)))
            retryAt = now + timedelta(minutes=5)
            targets = [(row.document_id, LexicalScope(ownerId=row.user_id, namespace=row.namespace)) for row in pending]
            for row in pending:
                row.index_cleanup_after = retryAt
        successes = 0
        for identity, scope in targets:
            try:
                if self._delete(scope, documentId=identity):
                    with self.sessions.begin() as db:
                        document = db.get(Document, identity, with_for_update=True)
                        if document is not None and document.status == "archived" and document.index_cleanup_after == retryAt:
                            document.index_cleanup_after = None
                    successes += 1
            except Exception as error:
                logger.warning("Archived document index cleanup deferred: document_id={}, error_type={}", identity, type(error).__name__)
        return CleanupBatch(claimed=len(targets), succeeded=successes)

    def _collectAttempts(self, *, attemptId: str | None = None) -> CleanupBatch:
        with self.sessions.begin() as db:
            now = LexicalCoordination.getDatabaseTime(db)
            query = select(DocumentEnrichmentPublication).where(
                DocumentEnrichmentPublication.state == "failed", DocumentEnrichmentPublication.cleanup_after <= now,
            )
            if attemptId is not None:
                query = query.where(DocumentEnrichmentPublication.id == attemptId)
            pending = list(db.scalars(query.order_by(DocumentEnrichmentPublication.cleanup_after,
                DocumentEnrichmentPublication.id).limit(20).with_for_update(skip_locked=True)))
            retryAt = now + timedelta(minutes=5)
            targets = [(row.id, LexicalScope(ownerId=row.user_id, namespace=row.namespace)) for row in pending]
            for row in pending:
                row.cleanup_after = retryAt
        successes = 0
        for identity, scope in targets:
            try:
                if self._delete(scope, attemptId=identity):
                    with self.sessions.begin() as db:
                        attempt = db.get(DocumentEnrichmentPublication, identity, with_for_update=True)
                        if attempt is not None and attempt.state == "failed" and attempt.cleanup_after == retryAt:
                            attempt.cleanup_after = None
                    successes += 1
            except Exception as error:
                logger.warning("Failed enrichment index cleanup deferred: attempt_id={}, error_type={}", identity, type(error).__name__)
        return CleanupBatch(claimed=len(targets), succeeded=successes)

    def _delete(
        self, scope: LexicalScope, *, documentId: str | None = None, attemptId: str | None = None,
    ) -> bool:
        with self.sessions.begin() as db:
            registry = db.get(LexicalNamespaceIndex, scope.fingerprint)
            if registry is None or registry.indexUuid is None:
                return True
        native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
        try:
            index = native.getLifecycleSnapshot(isExact=True, timeoutSeconds=5)
            if index.getIndexUuid(native.indexName) is None:
                return True
            if index.isClosed(native.indexName):
                LexicalCoordination(self.sessions).ensureIndex(scope=scope, native=native)
            if documentId is not None:
                return native.deleteDocument(documentId)
            return native.deleteBuild(attemptId) if attemptId is not None else False
        finally:
            native.close()
