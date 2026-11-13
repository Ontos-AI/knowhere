"""Prepare native manifests outside the transaction that activates them."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.lexicalpublishedmanifest import LexicalPublishedManifest
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.serving_generation import lock_namespace_generation
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


MANIFEST_READ_TIMEOUT_SECONDS = 30
MANIFEST_RETIREMENT_SECONDS = 60


class ManifestPublicationAdvanced(LexicalIndexError):
    """Another writer activated or changed the namespace during preparation."""


@dataclass(frozen=True)
class PreparedManifest:
    id: str
    scopeId: str
    previousId: str | None
    generation: int
    buildIds: tuple[str, ...]


class ManifestPublication:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def prepare(
        self, *, scope: LexicalScope, documentId: str | None, buildId: str | None,
        native: OpenSearchLexicalIndex,
    ) -> PreparedManifest:
        prepared = self.prepareMetadata(scope=scope, documentId=documentId, buildId=buildId)
        native.writePublishedManifest(manifestId=prepared.id, scopeId=prepared.scopeId, buildIds=prepared.buildIds)
        return prepared

    def prepareMetadata(
        self, *, scope: LexicalScope, documentId: str | None, buildId: str | None,
    ) -> PreparedManifest:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
        namespace = storage.RetrievalNamespaceGeneration
        document = storage.Document
        scopeId: str = scope.fingerprint
        with self.sessions.begin() as db:
            generationQuery = select(namespace.generation).where(
                namespace.user_id == scope.ownerId, namespace.namespace == scope.namespace,
            ).scalar_subquery()
            rows = db.execute(select(
                LexicalNamespaceIndex.publishedManifestId, func.coalesce(generationQuery, 0),
                document.document_id, document.current_indexing_build_id,
            ).select_from(LexicalNamespaceIndex).outerjoin(document, (
                (document.user_id == scope.ownerId) & (document.namespace == scope.namespace)
                & (document.status == "active") & document.current_job_result_id.is_not(None)
                & document.current_indexing_build_id.is_not(None)
            )).where(LexicalNamespaceIndex.id == scopeId)).tuples().all()
            if not rows:
                raise LexicalIndexError("Native namespace is absent during manifest preparation", isRetryable=True)
            previousId, generation, _, _ = rows[0]
            buildIds: set[str] = {
                currentBuild for _, _, currentDocument, currentBuild in rows
                if currentBuild is not None and currentDocument != documentId
            }
            if buildId is not None:
                buildIds.add(buildId)
            prepared = PreparedManifest(f"{scopeId}-{uuid4().hex}", scopeId, previousId, generation, tuple(sorted(buildIds)))
            db.add(LexicalPublishedManifest(id=prepared.id, namespaceIndexId=scopeId, state="prepared",
                cleanupAfter=LexicalCoordination.getDatabaseTime(db)
                + timedelta(seconds=settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS)))
        return prepared

    def seed(self, *, scope: LexicalScope, native: OpenSearchLexicalIndex, expectedEpoch: int | None = None) -> str:
        """Publish the current build list for a namespace that has no manifest yet."""
        prepared = self.prepareMetadata(scope=scope, documentId=None, buildId=None)
        native.writePublishedManifest(manifestId=prepared.id, scopeId=prepared.scopeId, buildIds=prepared.buildIds)
        with self.sessions.begin() as db:
            control = LexicalCoordination.lockControl(db)
            if expectedEpoch is not None and control.epoch != expectedEpoch:
                raise LexicalIndexError("OpenSearch epoch changed during manifest seeding", isRetryable=True)
            generation = lock_namespace_generation(db, user_id=scope.ownerId, namespace=scope.namespace)
            self.activate(db, prepared, previousGeneration=generation.generation, publishedGeneration=generation.generation)
        return prepared.id

    @staticmethod
    def activate(db: Session, prepared: PreparedManifest | None, *, previousGeneration: int, publishedGeneration: int) -> None:
        if prepared is None:
            return
        registry = db.get(LexicalNamespaceIndex, prepared.scopeId)
        if registry is None or registry.publishedManifestId != prepared.previousId or previousGeneration != prepared.generation:
            raise ManifestPublicationAdvanced("Another publication changed the namespace manifest", isRetryable=True)
        candidate = db.get(LexicalPublishedManifest, prepared.id, with_for_update=True)
        if candidate is None or candidate.state != "prepared":
            raise LexicalIndexError("Prepared namespace manifest is no longer available", isRetryable=True)
        if prepared.previousId is not None:
            previous = db.get(LexicalPublishedManifest, prepared.previousId, with_for_update=True)
            if previous is None or previous.state != "active":
                raise LexicalIndexError("Current namespace manifest is missing", isRetryable=True)
            previous.state = "retired"
            previous.cleanupAfter = None
        registry.publishedManifestId = prepared.id
        candidate.state = "active"
        candidate.generation = publishedGeneration
        candidate.cleanupAfter = None
