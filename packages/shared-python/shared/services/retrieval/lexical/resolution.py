"""Resolve lexical indexes whose last open/close outcome is unknown, without an operator.

An index becomes ``uncertain`` when the node did not answer a lifecycle request in
time, or when the registry and the node disagree. The slot stays charged and the
namespace refuses new work until it is resolved, so leaving it for a person blocks
that namespace and, at capacity, every new one.

Resolution drives the index to confirmed ``closed`` (see
``LexicalCoordination.resolveUnsettled``). It runs three ways:

- on commit: a row that becomes ``uncertain`` dispatches ``resolveIndex`` after a
  short delay, which retries itself up to ``_ATTEMPTS`` times;
- in the hourly sweep, for every unresolved row;
- only an index still unresolved after those attempts logs the
  ``lexical.index_lifecycle.unresolved`` error, which the Logfire error alert reports.
"""

import time
from typing import Final

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory
from shared.core.logging import LogEvent
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.lexical.coordination import LexicalCoordination, ResolutionOutcome
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.maintenance_signals import RESOLVE_INDEX_TASK, LexicalMaintenanceSignals
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope

# One dispatched attempt plus two self-scheduled retries before alerting.
_ATTEMPTS: Final[int] = 3
_RETRY_DELAY_SECONDS: Final[float] = 60
# Bounds one sweep; anything left waits for the next hour.
_SWEEP_BATCH: Final[int] = 50
_UNSETTLED_STATES: Final[tuple[str, ...]] = ("uncertain", "opening", "closing")
_ALERT_SAMPLE: Final[int] = 10


class LexicalIndexResolution:
    def __init__(self, *, sessionFactory: sessionmaker[Session] | None = None) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory or get_sync_session_factory()

    def resolveDispatched(self, registryId: str, hop: int) -> str:
        """Run attempt ``hop + 1``; schedule the next one, or alert after the last."""
        with LexicalMaintenanceSignals.suppress(RESOLVE_INDEX_TASK):
            outcome: ResolutionOutcome | None = self._attempt(registryId)
        if outcome in ("closed", "settled"):
            return outcome
        if hop + 1 < _ATTEMPTS:
            LexicalMaintenanceSignals.dispatch(RESOLVE_INDEX_TASK, registryId,
                countdown=_RETRY_DELAY_SECONDS, hop=hop + 1)
            return "rescheduled"
        if outcome == "pending":
            # Another issuer kept a live reservation throughout; it settles or expires.
            logger.warning("Lexical index resolution left to its live issuer: registry_id={}", registryId)
            return "pending"
        self._alert((registryId,), attempts=_ATTEMPTS)
        return "failed"

    def resolveDue(self, deadline: float) -> int:
        """Sweep: one attempt per unsettled index; one alert lists those still failing."""
        with self.sessions.begin() as db:
            registryIds: list[str] = list(db.scalars(select(LexicalNamespaceIndex.id).where(
                LexicalNamespaceIndex.state.in_(_UNSETTLED_STATES),
            ).order_by(LexicalNamespaceIndex.lastUsedAt, LexicalNamespaceIndex.id).limit(_SWEEP_BATCH)))
        resolved: int = 0
        failed: list[str] = []
        with LexicalMaintenanceSignals.suppress(RESOLVE_INDEX_TASK):
            for registryId in registryIds:
                if time.monotonic() >= deadline:
                    break
                outcome: ResolutionOutcome | None = self._attempt(registryId)
                if outcome is None:
                    failed.append(registryId)
                elif outcome == "closed":
                    resolved += 1
        if failed:
            self._alert(tuple(failed), attempts=None)
        return resolved

    def _attempt(self, registryId: str) -> ResolutionOutcome | None:
        """One resolution attempt; None means it failed and was logged."""
        with self.sessions.begin() as db:
            row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, registryId)
            if row is None or row.state not in _UNSETTLED_STATES:
                return "settled"
            ownerId, namespace, corpusKind = row.ownerId, row.namespace, row.corpusKind
        native: OpenSearchLexicalIndex | None = None
        try:
            if corpusKind not in ("private", "demo"):
                raise LexicalIndexError("Unresolved index has an invalid corpus identity")
            scope: LexicalScope = LexicalScope(ownerId=ownerId, namespace=namespace,
                corpusKind="demo" if corpusKind == "demo" else "private")
            if scope.fingerprint != registryId:
                raise LexicalIndexError("Unresolved index escaped its registered identity")
            native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL, scope=scope,
                indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
            outcome: ResolutionOutcome = LexicalCoordination(self.sessions).resolveUnsettled(
                scope=scope, native=native)
        except Exception as error:
            logger.warning("Lexical index resolution attempt failed: registry_id={} error_type={} error={}",
                registryId, type(error).__name__, str(error)[:200])
            return None
        finally:
            if native is not None:
                native.close()
        if outcome == "closed":
            logger.bind(event=LogEvent.LEXICAL_INDEX_LIFECYCLE_RESOLVED.value, registry_id=registryId).info(
                "Lexical index resolved to closed: registry_id={}", registryId)
        return outcome

    @staticmethod
    def _alert(registryIds: tuple[str, ...], *, attempts: int | None) -> None:
        logger.bind(event=LogEvent.LEXICAL_INDEX_LIFECYCLE_UNRESOLVED.value, unresolved_count=len(registryIds),
            registry_ids=list(registryIds[:_ALERT_SAMPLE]), attempts=attempts).error(
            "Lexical index stays unresolved after automatic resolution; its slot stays charged and its "
            "namespace refuses new work: count={} registry_ids={} attempts={}",
            len(registryIds), list(registryIds[:_ALERT_SAMPLE]), attempts if attempts is not None else "sweep")
