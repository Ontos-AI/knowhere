"""Lexical maintenance: per-item tasks dispatched on commit, plus an hourly safety sweep.

Work is normally dispatched when it is committed (see ``maintenance_signals``): one
task per retired or failed build, archived document, or failed enrichment attempt,
and one per index whose open/close outcome became unknown (see ``resolution``).
Each cleanup task claims its row with the same ``FOR UPDATE SKIP LOCKED`` lease as
the sweep, so a task and a sweep never process one row at the same time.

``maintainLexical`` runs hourly in the shared periodic database slot. It resolves
unsettled indexes, collects whatever a lost or failed dispatch left pending, and
runs index turnover as a safety net. Between sweeps nothing here queries the database unless work was committed.

Low-priority rollback-copy and retired-build work never gates completion.
"""

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.celery_app import celery_app
from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.models.database.document import Document
from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.services.redis.periodic_task_lock import periodic_task_lock
from shared.services.retrieval.lexical.additivecleanup import AdditiveCleanup
from shared.services.retrieval.lexical.cleanup import LexicalCleanup
from shared.services.retrieval.lexical.cleanupbatch import CleanupBatch
from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.maintenance_signals import (
    CLEAN_ARCHIVED_DOCUMENT_TASK, CLEAN_BUILD_TASK, CLEAN_FAILED_ATTEMPT_TASK, RESOLVE_INDEX_TASK,
    LexicalMaintenanceSignals,
)
from shared.services.retrieval.lexical.manifest_maintenance import collectPublishedManifests
from shared.services.retrieval.lexical.resolution import LexicalIndexResolution
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.turnover import turnOverIndexes

MAINTAIN_TASK: Final[str] = "shared.services.retrieval.lexical.maintenance.maintainLexical"
# Matches the hourly beat_schedule slot in celery_app.py.
_SWEEP_PERIOD_SECONDS: Final[int] = 3600
# The sweep drains due work in bounded passes and stops well before the next slot.
_SWEEP_BUDGET_SECONDS: Final[float] = 300
_CLAIM_LEASE: Final[timedelta] = timedelta(minutes=5)
# A task reschedules itself only for work due within the hour; the sweep owns the rest.
_RESCHEDULE_HORIZON: Final[timedelta] = timedelta(hours=1)
# Bounds reschedules for work that keeps declining (late writers, closed registries).
_MAXIMUM_HOPS: Final[int] = 3

_Outcome = str


@celery_app.task(name=MAINTAIN_TASK)
def maintainLexical() -> int:
    """Hourly safety sweep; a duplicate Beat firing in the same window is skipped."""
    with periodic_task_lock(MAINTAIN_TASK, period_seconds=_SWEEP_PERIOD_SECONDS) as acquired:
        if not acquired:
            return 0
        return sweepLexical()


def sweepLexical() -> int:
    """Drain every due maintenance row within the sweep budget."""
    deadline: float = time.monotonic() + _SWEEP_BUDGET_SECONDS
    isNativeServing: bool = settings.KNOWHERE_LEXICAL_BACKEND == "opensearch"
    sessions: sessionmaker[Session] = get_sync_session_factory()
    completed: int = 0
    while time.monotonic() < deadline:
        claimed: tuple[tuple[str, str, str], ...] | None = _claimPendingBuilds(sessions, isNativeServing)
        if claimed is None:
            return completed
        if not claimed:
            break
        completed += _processBuilds(sessions, claimed, isNativeServing)
    if isNativeServing:
        # First, so a resolved index frees its slot before turnover counts capacity.
        completed += _runSafely("Index resolution",
            lambda: LexicalIndexResolution(sessionFactory=sessions).resolveDue(deadline))
        completed += _drain("Additive index", lambda: AdditiveCleanup(sessions).runBatch(), deadline)
        completed += _runSafely("Empty namespace", lambda: LexicalCleanup(sessionFactory=sessions).closeEmptyNamespaces())
        completed += _runSafely("Index turnover", turnOverIndexes)
    completed += _runSafely("Manifest", lambda: collectPublishedManifests(sessions))
    return completed


@celery_app.task(name=CLEAN_BUILD_TASK)
def cleanBuild(buildId: str, hop: int = 0) -> _Outcome:
    """Collect one retired or failed build once it is due."""
    sessions: sessionmaker[Session] = get_sync_session_factory()
    with sessions.begin() as db:
        build: LexicalBuild | None = db.scalar(select(LexicalBuild).where(
            LexicalBuild.id == buildId, LexicalBuild.cleanupState == "pending").with_for_update(skip_locked=True))
        if build is None:
            return "skipped"
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        if build.nextMaintenanceAt is not None and build.nextMaintenanceAt > now:
            return _reschedule(CLEAN_BUILD_TASK, buildId, build.nextMaintenanceAt - now, hop)
        build.nextMaintenanceAt = now + _CLAIM_LEASE
        namespaceIndexId: str = build.namespaceIndexId
    cleanup: LexicalCleanup = LexicalCleanup(sessionFactory=sessions)
    try:
        isComplete: bool = cleanup.collect(buildId)
    except Exception as error:
        logger.error("Lexical build cleanup deferred: build_id={}, error_type={}", buildId, type(error).__name__)
        isComplete = False
    if not isComplete:
        return _rescheduleWhenDue(sessions, buildId, hop)
    _runSafely("Empty namespace", lambda: int(cleanup.closeEmptyNamespace(namespaceIndexId)))
    return "completed"


@celery_app.task(name=CLEAN_ARCHIVED_DOCUMENT_TASK)
def cleanArchivedDocument(documentId: str, hop: int = 0) -> _Outcome:
    """Remove one archived document from its index and close an empty namespace."""
    sessions: sessionmaker[Session] = get_sync_session_factory()
    with sessions.begin() as db:
        document: Document | None = db.get(Document, documentId)
        if document is None or document.status != "archived" or document.index_cleanup_after is None:
            return "skipped"
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        if document.index_cleanup_after > now:
            return _reschedule(CLEAN_ARCHIVED_DOCUMENT_TASK, documentId, document.index_cleanup_after - now, hop)
        namespaceIndexId: str = LexicalScope(ownerId=document.user_id, namespace=document.namespace).fingerprint
    if not AdditiveCleanup(sessions).collectDocument(documentId):
        return _reschedule(CLEAN_ARCHIVED_DOCUMENT_TASK, documentId, _CLAIM_LEASE, hop)
    _runSafely("Empty namespace", lambda: int(
        LexicalCleanup(sessionFactory=sessions).closeEmptyNamespace(namespaceIndexId)))
    return "completed"


@celery_app.task(name=CLEAN_FAILED_ATTEMPT_TASK)
def cleanFailedAttempt(attemptId: str, hop: int = 0) -> _Outcome:
    """Remove one failed enrichment attempt's records from its index once it is due."""
    sessions: sessionmaker[Session] = get_sync_session_factory()
    with sessions.begin() as db:
        attempt: DocumentEnrichmentPublication | None = db.get(DocumentEnrichmentPublication, attemptId)
        if attempt is None or attempt.state != "failed" or attempt.cleanup_after is None:
            return "skipped"
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        if attempt.cleanup_after > now:
            return _reschedule(CLEAN_FAILED_ATTEMPT_TASK, attemptId, attempt.cleanup_after - now, hop)
    if not AdditiveCleanup(sessions).collectAttempt(attemptId):
        return _reschedule(CLEAN_FAILED_ATTEMPT_TASK, attemptId, _CLAIM_LEASE, hop)
    return "completed"


@celery_app.task(name=RESOLVE_INDEX_TASK)
def resolveIndex(registryId: str, hop: int = 0) -> _Outcome:
    """Resolve one index whose last open/close outcome is unknown; alerts after the last attempt."""
    if settings.KNOWHERE_LEXICAL_BACKEND != "opensearch":
        return "skipped"
    return LexicalIndexResolution().resolveDispatched(registryId, hop)


def _claimPendingBuilds(sessions: sessionmaker[Session],
                        isNativeServing: bool) -> tuple[tuple[str, str, str], ...] | None:
    """Claim one batch of due builds; None when the backend is not serving."""
    with sessions.begin() as db:
        control: LexicalBackendControl | None = db.get(
            LexicalBackendControl, 1, with_for_update={"read": True},
        )
        if control is None or control.backend != settings.KNOWHERE_LEXICAL_BACKEND or control.mode != "serving":
            return None
        if not isNativeServing and control.clusterUuid is None:
            return None
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        pending = (or_(LexicalBuild.legacyCopyState == "pending", LexicalBuild.cleanupState == "pending")
            if isNativeServing else (
                (LexicalBuild.state == "retired") & (LexicalBuild.cleanupState == "pending")
                & LexicalBuild.retainForManifestReads.is_(True)
            ))
        rows: list[LexicalBuild] = list(db.scalars(select(LexicalBuild)
                .where(pending)
                .where(or_(LexicalBuild.nextMaintenanceAt.is_(None), LexicalBuild.nextMaintenanceAt <= now))
                .order_by(LexicalBuild.nextMaintenanceAt.asc().nullsfirst(), LexicalBuild.createdAt, LexicalBuild.id)
                .limit(20).with_for_update(skip_locked=True)))
        for row in rows:
            row.nextMaintenanceAt = now + _CLAIM_LEASE
        return tuple((row.id, row.legacyCopyState, row.cleanupState) for row in rows)


def _processBuilds(sessions: sessionmaker[Session], claimed: tuple[tuple[str, str, str], ...],
                   isNativeServing: bool) -> int:
    completed: int = 0
    for identity, copyState, cleanupState in claimed:
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
    return completed


def _drain(label: str, runBatch: Callable[[], CleanupBatch], deadline: float) -> int:
    """Repeat a bounded pass while it still claims due rows and the budget lasts."""
    completed: int = 0
    while time.monotonic() < deadline:
        try:
            batch: CleanupBatch = runBatch()
        except Exception as error:
            logger.error("{} maintenance deferred: error_type={}", label, type(error).__name__)
            break
        completed += batch.succeeded
        if batch.claimed == 0:
            break
    return completed


def _runSafely(label: str, operation: Callable[[], int]) -> int:
    try:
        return operation()
    except Exception as error:
        logger.error("{} maintenance deferred: error_type={}", label, type(error).__name__)
        return 0


def _reschedule(taskName: str, identity: str, delay: timedelta, hop: int) -> _Outcome:
    """Send the task again when its row is due; far or repeated work waits for the sweep."""
    if hop >= _MAXIMUM_HOPS or delay > _RESCHEDULE_HORIZON:
        return "deferred"
    LexicalMaintenanceSignals.dispatch(taskName, identity, countdown=max(1.0, delay.total_seconds()), hop=hop + 1)
    return "deferred"


def _rescheduleWhenDue(sessions: sessionmaker[Session], buildId: str, hop: int) -> _Outcome:
    with sessions.begin() as db:
        build: LexicalBuild | None = db.get(LexicalBuild, buildId)
        if build is None or build.cleanupState != "pending":
            return "completed"
        now: datetime = LexicalCoordination.getDatabaseTime(db)
        dueAt: datetime = build.nextMaintenanceAt or now + _CLAIM_LEASE
    return _reschedule(CLEAN_BUILD_TASK, buildId, max(dueAt - now, timedelta(seconds=1)), hop)
