"""Reclaim old and unused native manifests without waiting for active searches."""

from datetime import timedelta

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.lexicalpublishedmanifest import LexicalPublishedManifest
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.manifest_publication import MANIFEST_RETIREMENT_SECONDS
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


def collectPublishedManifests(sessions: sessionmaker[Session], *, limit: int = 20) -> int:
    scheduled: list[tuple[str, LexicalScope]] = []
    with sessions.begin() as db:
        control = LexicalCoordination.lockControl(db)
        now = LexicalCoordination.getDatabaseTime(db)
        registry = LexicalNamespaceIndex
        manifest = LexicalPublishedManifest
        rows = db.execute(select(manifest, registry).join(
            registry, registry.id == manifest.namespaceIndexId,
        ).where(
            or_(registry.publishedManifestId.is_(None), registry.publishedManifestId != manifest.id),
            or_(
                and_(manifest.state == "retired", manifest.cleanupAfter.is_(None)),
                and_(manifest.state.in_(("prepared", "retired", "cleaning")), manifest.cleanupAfter <= now),
            ),
        ).order_by(manifest.cleanupAfter.asc().nullsfirst(), manifest.id).limit(limit)
            .with_for_update(skip_locked=True, of=manifest)).tuples().all()
        for record, index in rows:
            if record.cleanupAfter is None:
                # Start the safety period only after the retirement commit is visible.
                record.cleanupAfter = now + timedelta(seconds=MANIFEST_RETIREMENT_SECONDS)
                continue
            record.state = "cleaning"
            record.cleanupAfter = now + timedelta(minutes=5)
            scheduled.append((record.id, LexicalScope(ownerId=index.ownerId, namespace=index.namespace,
                corpusKind="demo" if index.corpusKind == "demo" else "private")))
        epoch: int = control.epoch
        clusterUuid: str | None = control.clusterUuid
    completed: int = 0
    for identity, scope in scheduled:
        native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
        try:
            if not identity.startswith(scope.fingerprint + "-"):
                raise ValueError("Invalid retired manifest identity")
            if clusterUuid is None or native.getClusterUuid() != clusterUuid:
                raise ValueError("Manifest cleanup resolved a different native cluster")
            native.deletePublishedManifest(identity)
            with sessions.begin() as db:
                control = LexicalCoordination.lockControl(db)
                if control.epoch == epoch:
                    record = db.get(LexicalPublishedManifest, identity, with_for_update=True)
                    if record is not None and record.state == "cleaning":
                        db.delete(record)
                        completed += 1
        except Exception as error:
            logger.warning("Manifest cleanup deferred: manifest_id={}, error_type={}", identity, type(error).__name__)
        finally:
            native.close()
    return completed
