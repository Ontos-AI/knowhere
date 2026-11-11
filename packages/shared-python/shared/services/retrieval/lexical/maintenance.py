"""Low-priority rollback-copy and retired-build work never gates completion."""

from datetime import timedelta

from loguru import logger
from sqlalchemy import func, or_, select

from shared.core.celery_app import celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.services.retrieval.lexical.cleanup import LexicalCleanup
from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.manifest_maintenance import collectPublishedManifests


@celery_app.task(name="shared.services.retrieval.lexical.maintenance.maintainLexical")
def maintainLexical() -> int:
    isNativeServing: bool = settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"
    if not isNativeServing and not settings.KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS:
        return 0
    sessions = get_sync_session_factory()
    with sessions.begin() as db:
        control: LexicalBackendControl | None = db.get(
            LexicalBackendControl, 1, with_for_update={"read": True},
        )
        if control is None or control.backend != settings.KNOWHERE_LEXICAL_BACKEND or control.mode != "serving":
            return 0
        if not isNativeServing and control.clusterUuid is None:
            return 0
        now = LexicalCoordination.getDatabaseTime(db)
        pending = (or_(LexicalBuild.legacyCopyState == "pending", LexicalBuild.cleanupState == "pending")
            if isNativeServing else (
                (LexicalBuild.state == "retired") & (LexicalBuild.cleanupState == "pending")
                & LexicalBuild.retainForManifestReads.is_(True)
            ))
        rows = list(db.scalars(select(LexicalBuild)
                .where(pending)
                .where(or_(LexicalBuild.nextMaintenanceAt.is_(None), LexicalBuild.nextMaintenanceAt <= now))
                .order_by(LexicalBuild.nextMaintenanceAt.asc().nullsfirst(), LexicalBuild.createdAt, LexicalBuild.id)
                .limit(20).with_for_update(skip_locked=True)))
        identities: tuple[tuple[str, str, str], ...] = tuple((row.id, row.legacyCopyState, row.cleanupState) for row in rows)
        for row in rows:
            row.nextMaintenanceAt = now + timedelta(minutes=5)
    completed: int = 0
    for identity, copyState, cleanupState in identities:
        if isNativeServing and copyState == "pending":
            try:
                completed += int(LegacyCopyDispatch.submit(identity))
            except Exception as error:
                logger.error("PG copy maintenance deferred: build_id={}, error_type={}", identity, type(error).__name__)
        if cleanupState == "pending":
            try:
                completed += int(LexicalCleanup(sessionFactory=sessions).collect(identity))
            except Exception as error:
                logger.error("Lexical build cleanup deferred: build_id={}, error_type={}", identity, type(error).__name__)
    if isNativeServing:
        try:
            completed += LexicalInputCollector(sessionFactory=sessions).run()
        except Exception as error:
            logger.error("Indexing input maintenance deferred: error_type={}", type(error).__name__)
        try:
            completed += LexicalCleanup(sessionFactory=sessions).closeEmptyNamespaces()
        except Exception as error:
            logger.error("Empty namespace maintenance deferred: error_type={}", type(error).__name__)
    try:
        completed += collectPublishedManifests(sessions)
    except Exception as error:
        logger.error("Manifest maintenance deferred: error_type={}", type(error).__name__)
    with sessions.begin() as db:
        pendingCopies, oldestCopy = db.execute(select(func.count(), func.min(LexicalBuild.createdAt))
            .where(LexicalBuild.legacyCopyState == "pending")).one()
        pendingCleanup, oldestCleanup = db.execute(select(func.count(), func.min(LexicalBuild.createdAt))
            .where(LexicalBuild.cleanupState == "pending")).one()
    logger.info("Lexical maintenance backlog: pending_copies={} oldest_copy={} pending_cleanup={} oldest_cleanup={} batch_limit=20 cooldown_seconds=300",
        pendingCopies, oldestCopy, pendingCleanup, oldestCleanup)
    return completed
