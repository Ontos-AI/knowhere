"""Short SQL decisions around native operations; no SQL lock spans an RPC."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import time
from typing import Literal
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicallease import LexicalLease
from shared.models.database.job import Job
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.stage_observer import StageObserver


_OPEN_LIMIT: int = 400
_WAIT_SECONDS: float = 5.0
_OPERATION_EXPIRY: timedelta = timedelta(seconds=15)
_CHARGED_STATES: frozenset[str] = frozenset({"opening", "open", "closing", "uncertain"})


@dataclass(frozen=True)
class _Observation:
    startedAt: datetime
    incarnation: str | None
    epoch: int


@dataclass(frozen=True)
class _Operation:
    scope: LexicalScope
    identity: str
    indexName: str
    indexUuid: str | None
    incarnation: str
    clusterUuid: str
    epoch: int
    action: Literal["create", "open", "close"]


class _CapacityWait:
    """A full charged registry requires a slot, not only a same-scope poll."""


_CAPACITY_WAIT: _CapacityWait = _CapacityWait()


class LexicalCoordination:
    def __init__(self, sessionFactory: sessionmaker[Session]) -> None:
        self.sessions: sessionmaker[Session] = sessionFactory

    @staticmethod
    def lockControl(db: Session, *, isExclusive: bool = False, requiresServing: bool = True) -> LexicalBackendControl:
        control: LexicalBackendControl | None = db.scalars(
            select(LexicalBackendControl)
            .where(LexicalBackendControl.id == 1)
            .with_for_update(read=not isExclusive)
        ).one_or_none()
        if control is None:
            # Only bootstrap needs an INSERT. Concurrent initializers still
            # converge through the unique row before taking its serving gate.
            db.execute(insert(LexicalBackendControl).values(
                id=1, backend=settings.KNOWHERE_LEXICAL_BACKEND, mode="serving", epoch=1,
            ).on_conflict_do_nothing(index_elements=["id"]))
            control = db.scalars(select(LexicalBackendControl).where(
                LexicalBackendControl.id == 1).with_for_update(read=not isExclusive)).one()
        if requiresServing and (
            control.backend != settings.KNOWHERE_LEXICAL_BACKEND
            or control.mode != "serving"
        ):
            raise LexicalIndexError(
                "Lexical backend transition is in progress", isRetryable=True
            )
        return control

    @staticmethod
    def getDatabaseTime(db: Session) -> datetime:
        return db.scalars(select(func.clock_timestamp())).one()

    def ensureIndex(
        self, *, scope: LexicalScope, native: OpenSearchLexicalIndex
    ) -> tuple[str, int]:
        deadline: float = time.monotonic() + _WAIT_SECONDS
        observation: _Observation = self._beginObservation(deadline)
        exact: NativeLifecycleSnapshot = native.getLifecycleSnapshot(
            isExact=True, timeoutSeconds=self._remaining(deadline)
        )
        observed: tuple[str, int] | None = self.observeIndex(scope=scope, indexName=native.indexName,
            observation=observation, exact=exact, deadline=deadline)
        if observed is not None:
            return observed
        while True:
            observation = self._beginObservation(deadline)
            inventory: NativeLifecycleSnapshot = native.getLifecycleSnapshot(
                isExact=False, timeoutSeconds=self._remaining(deadline)
            )
            decision: _Operation | tuple[str, int] | LexicalIndexError | _CapacityWait | None = (
                self._allocate(
                    scope, native.indexName, observation, inventory, deadline=deadline
                )
            )
            if isinstance(decision, LexicalIndexError):
                raise decision
            if isinstance(decision, tuple):
                return decision
            if decision is None:
                time.sleep(min(0.05, self._remaining(deadline)))
                continue
            if isinstance(decision, _CapacityWait):
                with StageObserver.measure("capacity_wait_ms"):
                    time.sleep(min(0.05, self._remaining(deadline)))
                continue
            operationDeadline: float = (
                deadline
                if decision.action == "close"
                else time.monotonic() + _WAIT_SECONDS
            )
            if decision.action == "close":
                with StageObserver.measure("capacity_wait_ms", intervalName="lru_intervals_utc"):
                    identity: str = self._execute(native, decision, deadline=operationDeadline)
            else:
                identity = self._execute(native, decision, deadline=operationDeadline)
            if decision.action != "close":
                return identity, decision.epoch

    def observeIndex(self, *, scope: LexicalScope, indexName: str,
                     observation: _Observation, exact: NativeLifecycleSnapshot,
                     deadline: float) -> tuple[str, int] | None:
        with self._transaction(deadline) as db:
            control: LexicalBackendControl = self.lockControl(db)
            self._checkObservation(control, observation, exact)
            row: LexicalNamespaceIndex | None = db.scalar(
                select(LexicalNamespaceIndex)
                .where(LexicalNamespaceIndex.id == scope.fingerprint)
                .with_for_update()
            )
            if (
                row is not None
                and control.nodeIncarnation == exact.getIncarnation()
                and row.state == "open"
                and row.indexName == indexName
                and row.indexUuid is not None
                and row.indexUuid == exact.getIndexUuid(row.indexName)
                and exact.isReady(row.indexName)
            ):
                row.lastUsedAt = self.getDatabaseTime(db)
                return row.indexUuid, control.epoch
        return None

    def trimIdle(self, native: OpenSearchLexicalIndex) -> int:
        """Drive one bounded high-to-low sweep using the same close reservations."""
        high: int = settings.KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK
        low: int = settings.KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK
        closed: int = 0
        while closed < high - low:
            deadline: float = time.monotonic() + _WAIT_SECONDS
            observation: _Observation = self._beginObservation(deadline)
            inventory: NativeLifecycleSnapshot = native.getLifecycleSnapshot(
                isExact=False, timeoutSeconds=self._remaining(deadline)
            )
            with self._transaction(deadline) as db:
                control: LexicalBackendControl = self.lockControl(db, isExclusive=True)
                rows: list[LexicalNamespaceIndex] = self._reconcile(
                    db, control, observation, inventory
                )
                occupied: int = self._countOccupied(rows, inventory)
                closing: int = sum(row.state == "closing" for row in rows)
                if (closed == 0 and occupied < high) or occupied - closing <= low:
                    break
                operation: _Operation | None = self._reserveVictim(
                    db, control, inventory
                )
            if operation is None:
                break
            self._execute(native, operation, deadline=deadline)
            closed += 1
        return closed

    def closeEmpty(self, *, scope: LexicalScope, native: OpenSearchLexicalIndex) -> bool:
        """Close an empty, settled scope through the ordinary lifecycle fence.

        The registry and its native UUID survive. A later admission opens the
        same index; an absent previously established index remains a fault.
        """
        deadline: float = time.monotonic() + _WAIT_SECONDS
        with self._transaction(deadline) as db:
            if not self._canCloseEmpty(db, scope):
                return False
        observation: _Observation = self._beginObservation(deadline)
        inventory: NativeLifecycleSnapshot = native.getLifecycleSnapshot(
            isExact=False, timeoutSeconds=self._remaining(deadline))
        with self._transaction(deadline) as db:
            control: LexicalBackendControl = self.lockControl(db, isExclusive=True)
            rows: list[LexicalNamespaceIndex] = self._reconcile(db, control, observation, inventory)
            row: LexicalNamespaceIndex | None = next((item for item in rows if item.id == scope.fingerprint), None)
            if row is None or row.state == "closed":
                return False
            if (row.state != "open" or row.indexUuid is None
                or inventory.getIndexUuid(row.indexName) != row.indexUuid or not inventory.isOpen(row.indexName)):
                raise LexicalIndexError("Empty namespace lifecycle identity is unresolved", isRetryable=True)
            if not self._canCloseEmpty(db, scope):
                return False
            operation: _Operation = self._reserve(db, control, scope, row, "close")
        self._execute(native, operation, deadline=deadline)
        return True

    @staticmethod
    def _canCloseEmpty(db: Session, scope: LexicalScope) -> bool:
        storage: CorpusStorage = CorpusStorage.resolve_namespace(scope.namespace)
        scopeFilters = (
            storage.Document.user_id == scope.ownerId,
            storage.Document.namespace == scope.namespace,
            storage.Document.status == "active",
        )
        if db.scalar(select(storage.Document.document_id).where(*scopeFilters,
            storage.Document.current_job_result_id.is_not(None)).limit(1)) is not None:
            return False
        if db.scalar(select(Job.job_id).join(storage.Document,
            storage.Document.accepted_job_id == Job.job_id).where(*scopeFilters,
            Job.status.in_(("pending", "running"))).limit(1)) is not None:
            return False
        if db.scalar(select(LexicalBuild.id).where(LexicalBuild.namespaceIndexId == scope.fingerprint,
            (LexicalBuild.state == "writing") | (LexicalBuild.cleanupState == "pending")).limit(1)) is not None:
            return False
        return db.scalar(select(LexicalLease.id).where(LexicalLease.namespaceIndexId == scope.fingerprint,
            LexicalLease.state != "released").limit(1)) is None

    def _beginObservation(self, deadline: float) -> _Observation:
        with self._transaction(deadline) as db:
            control: LexicalBackendControl = self.lockControl(db)
            return _Observation(
                self.getDatabaseTime(db), control.nodeIncarnation, control.epoch
            )

    @staticmethod
    def _checkObservation(
        control: LexicalBackendControl,
        observation: _Observation,
        inventory: NativeLifecycleSnapshot,
    ) -> None:
        incarnation: str = inventory.getIncarnation()
        if control.clusterUuid not in (None, inventory.cluster_uuid):
            raise LexicalIndexError("Unexpected native cluster identity")
        if control.epoch != observation.epoch or control.nodeIncarnation not in (
            observation.incarnation,
            incarnation,
        ):
            raise LexicalIndexError(
                "Native lifecycle observation was superseded", isRetryable=True
            )

    def _reconcile(
        self,
        db: Session,
        control: LexicalBackendControl,
        observation: _Observation,
        inventory: NativeLifecycleSnapshot,
    ) -> list[LexicalNamespaceIndex]:
        self._checkObservation(control, observation, inventory)
        incarnation: str = inventory.getIncarnation()
        hasRestarted: bool = control.nodeIncarnation not in (None, incarnation)
        control.clusterUuid = inventory.cluster_uuid
        control.nodeIncarnation = incarnation
        rows: list[LexicalNamespaceIndex] = list(
            db.scalars(select(LexicalNamespaceIndex))
        )
        now: datetime = self.getDatabaseTime(db)
        for row in rows:
            # An inventory response is not evidence about a later reservation or acknowledgment.
            if row.lastUsedAt > observation.startedAt:
                continue
            identity: str | None = inventory.getIndexUuid(row.indexName)
            isUnresolved: bool = (
                (
                    row.state in ("opening", "closing")
                    and (hasRestarted or now - row.lastUsedAt > _OPERATION_EXPIRY)
                )
                or (
                    row.state == "open"
                    and (
                        row.indexUuid != identity or not inventory.isOpen(row.indexName)
                    )
                )
                or (
                    row.state == "closed"
                    and (
                        row.indexUuid != identity
                        or not inventory.isClosed(row.indexName)
                    )
                )
                or (row.state == "uncreated" and identity is not None)
            )
            if isUnresolved:
                self._markUncertain(row)
        return rows

    @staticmethod
    def _countOccupied(
        rows: list[LexicalNamespaceIndex], inventory: NativeLifecycleSnapshot
    ) -> int:
        charged: int = sum(row.state in _CHARGED_STATES for row in rows)
        registered: set[str] = {row.indexName for row in rows}
        # OpenSearch plugins can create non-dot indexes. The configured prefix
        # reserves the business index space, including unmanaged occupants.
        prefix: str = f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-"
        unmanaged: set[str] = {
            name
            for name in inventory.metadata.indices
            if name.startswith(prefix)
            and inventory.isOpen(name)
            and name not in registered
        }
        return charged + len(unmanaged)

    def _allocate(
        self,
        scope: LexicalScope,
        indexName: str,
        observation: _Observation,
        inventory: NativeLifecycleSnapshot,
        *,
        deadline: float,
    ) -> _Operation | tuple[str, int] | LexicalIndexError | _CapacityWait | None:
        with self._transaction(deadline) as db:
            control: LexicalBackendControl = self.lockControl(db, isExclusive=True)
            rows: list[LexicalNamespaceIndex] = self._reconcile(
                db, control, observation, inventory
            )
            self._remaining(deadline)
            row: LexicalNamespaceIndex | None = db.get(
                LexicalNamespaceIndex, scope.fingerprint
            )
            if row is not None and row.indexName != indexName:
                return LexicalIndexError(
                    "Native namespace name differs from its registry"
                )
            if row is not None and row.state == "open":
                if (
                    row.indexUuid is not None
                    and row.indexUuid == inventory.getIndexUuid(indexName)
                    and inventory.isReady(indexName)
                ):
                    row.lastUsedAt = self.getDatabaseTime(db)
                    return row.indexUuid, control.epoch
                return LexicalIndexError(
                    "Native index primary is unavailable", isRetryable=True
                )
            if row is not None and row.state == "uncertain":
                return LexicalIndexError(
                    "Native index operation requires reconciliation", isRetryable=True
                )
            if row is not None and row.state not in ("closed", "uncreated"):
                return None
            if row is not None and row.lastUsedAt > observation.startedAt:
                return None
            if inventory.getIndexUuid(indexName) is not None and (
                row is None or row.state == "uncreated"
            ):
                return LexicalIndexError(
                    "Native index has no matching registry ownership"
                )
            if self._countOccupied(rows, inventory) >= _OPEN_LIMIT:
                if any(item.state == "closing" for item in rows):
                    return _CAPACITY_WAIT
                return self._reserveVictim(db, control, inventory) or _CAPACITY_WAIT
            if row is None:
                row = LexicalNamespaceIndex(
                    id=scope.fingerprint,
                    ownerId=scope.ownerId,
                    namespace=scope.namespace,
                    corpusKind=scope.corpusKind,
                    indexName=indexName,
                    state="uncreated",
                )
                db.add(row)
            return self._reserve(
                db, control, scope, row, "open" if row.state == "closed" else "create"
            )

    def _reserveVictim(
        self,
        db: Session,
        control: LexicalBackendControl,
        inventory: NativeLifecycleSnapshot,
    ) -> _Operation | None:
        confirmed: set[str] = {
            name for name in inventory.metadata.indices if inventory.isOpen(name)
        }
        candidates = db.scalars(
            select(LexicalNamespaceIndex)
            .where(
                LexicalNamespaceIndex.state == "open",
                LexicalNamespaceIndex.indexName.in_(confirmed),
            )
            .order_by(LexicalNamespaceIndex.lastUsedAt, LexicalNamespaceIndex.id)
            .with_for_update()
        )
        for row in candidates:
            if not row.indexUuid or row.indexUuid != inventory.getIndexUuid(
                row.indexName
            ):
                continue
            if row.corpusKind not in ("private", "demo"):
                self._markUncertain(row)
                continue
            scope: LexicalScope = LexicalScope(
                ownerId=row.ownerId,
                namespace=row.namespace,
                corpusKind="demo" if row.corpusKind == "demo" else "private",
            )
            if scope.fingerprint != row.id:
                self._markUncertain(row)
                continue
            return self._reserve(db, control, scope, row, "close")
        return None

    def _reserve(
        self,
        db: Session,
        control: LexicalBackendControl,
        scope: LexicalScope,
        row: LexicalNamespaceIndex,
        action: Literal["create", "open", "close"],
    ) -> _Operation:
        assert control.nodeIncarnation is not None and control.clusterUuid is not None
        identity: str = str(uuid4())
        row.state = "closing" if action == "close" else "opening"
        row.operationId = identity
        row.lastUsedAt = self.getDatabaseTime(db)
        return _Operation(
            scope,
            identity,
            row.indexName,
            row.indexUuid,
            control.nodeIncarnation,
            control.clusterUuid,
            control.epoch,
            action,
        )

    def _execute(
        self, native: OpenSearchLexicalIndex, operation: _Operation, *, deadline: float
    ) -> str:
        target: OpenSearchLexicalIndex = native.forScope(operation.scope)
        try:
            if target.indexName != operation.indexName:
                raise LexicalIndexError(
                    "Native lifecycle target differs from its registry"
                )
            before: NativeLifecycleSnapshot = target.getLifecycleSnapshot(
                isExact=True, timeoutSeconds=self._remaining(deadline)
            )
            self._checkNativeIdentity(before, operation)
            if before.getIndexUuid(target.indexName) != operation.indexUuid:
                raise LexicalIndexError("Native lifecycle target identity changed")
            if operation.action == "create":
                target.createIndex(
                    operationId=operation.identity,
                    timeoutSeconds=self._remaining(deadline),
                )
            else:
                if (
                    operation.action == "close"
                    and not before.isOpen(target.indexName)
                    or operation.action == "open"
                    and not before.isClosed(target.indexName)
                ):
                    raise LexicalIndexError("Native lifecycle target state changed")
                target.setIndexOpen(
                    isOpen=operation.action == "open",
                    operationId=operation.identity,
                    timeoutSeconds=self._remaining(deadline),
                )
            after: NativeLifecycleSnapshot = target.getLifecycleSnapshot(
                isExact=True, timeoutSeconds=self._remaining(deadline)
            )
            self._checkNativeIdentity(after, operation)
            identity: str | None = after.getIndexUuid(target.indexName)
            if (
                not identity
                or operation.indexUuid not in (None, identity)
                or not (
                    after.isClosed(target.indexName)
                    if operation.action == "close"
                    else after.isReady(target.indexName)
                )
            ):
                raise LexicalIndexError(
                    "Native lifecycle completion could not be verified"
                )
            self._finish(operation, identity, deadline=deadline)
            return identity
        except Exception as error:
            isTimeout: bool = isinstance(error, TimeoutError) or (
                isinstance(error, LexicalIndexError) and error.isTimeout
            )
            try:
                self._finish(operation, None, deadline=deadline)
            except Exception as settlementError:
                raise LexicalIndexError(
                    "Native lifecycle settlement requires reconciliation",
                    isRetryable=True, hasUnknownOutcome=True, isTimeout=isTimeout,
                ) from settlementError
            raise LexicalIndexError(
                "Native index operation requires reconciliation",
                isRetryable=True,
                hasUnknownOutcome=True,
                isTimeout=isTimeout,
            ) from error
        finally:
            target.close()

    @staticmethod
    def _checkNativeIdentity(
        inventory: NativeLifecycleSnapshot, operation: _Operation
    ) -> None:
        if (
            inventory.cluster_uuid != operation.clusterUuid
            or inventory.getIncarnation() != operation.incarnation
        ):
            raise LexicalIndexError(
                "Native node restarted during index lifecycle operation"
            )

    def _finish(
        self, operation: _Operation, identity: str | None, *, deadline: float
    ) -> None:
        # A short settlement attempt may still journal uncertainty after a native timeout.
        # If the SQL gate is locked, the pending reservation remains charged.
        with self._transaction(max(deadline, time.monotonic() + 0.1)) as db:
            control: LexicalBackendControl = self.lockControl(db, isExclusive=True)
            row: LexicalNamespaceIndex = db.scalars(
                select(LexicalNamespaceIndex)
                .where(LexicalNamespaceIndex.id == operation.scope.fingerprint)
                .with_for_update()
            ).one()
            expected: str = "closing" if operation.action == "close" else "opening"
            if row.operationId != operation.identity or row.state != expected:
                if identity is not None:
                    raise LexicalIndexError(
                        "Native lifecycle operation lost its reservation"
                    )
                return
            if (
                identity is None
                or control.epoch != operation.epoch
                or control.nodeIncarnation != operation.incarnation
            ):
                self._markUncertain(row)
                if identity is None:
                    return
            else:
                row.state = "closed" if operation.action == "close" else "open"
                row.indexUuid = identity
                row.operationId = None
                row.lastUsedAt = self.getDatabaseTime(db)
                return
        raise LexicalIndexError(
            "Native lifecycle operation crossed a node or epoch change"
        )

    @staticmethod
    def _markUncertain(row: LexicalNamespaceIndex) -> None:
        logger.error(
            "Native index lifecycle unresolved: registry_id={}, state={}, operation_id={}",
            row.id,
            row.state,
            row.operationId,
        )
        row.state = "uncertain"

    @contextmanager
    def _transaction(self, deadline: float) -> Iterator[Session]:
        try:
            with self.sessions.begin() as db:
                timeout: str = f"{max(1, int(self._remaining(deadline) * 1000))}ms"
                db.execute(
                    select(
                        func.set_config("lock_timeout", timeout, True),
                        func.set_config("statement_timeout", timeout, True),
                    )
                )
                yield db
        except DBAPIError as error:
            if getattr(error.orig, "pgcode", None) in ("55P03", "57014"):
                raise LexicalIndexError(
                    "Native index admission timed out", isRetryable=True
                ) from error
            raise

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining: float = deadline - time.monotonic()
        if remaining <= 0:
            raise LexicalIndexError(
                "Native index admission timed out", isRetryable=True
            )
        return remaining
