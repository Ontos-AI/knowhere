"""Owned PostgreSQL checks: unknown close outcomes are retried, resolved, and alerted."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess, clear_application_modules, configure_contract_environment,
    get_contract_database_url, prepare_contract_storage,
)

_CloseBehaviour = Literal["normal", "unanswered", "lostResponse"]


@dataclass
class FakeNode:
    """One OpenSearch node: index state, plus how it answers the next close requests."""

    uuids: dict[str, str] = field(default_factory=dict)
    openNames: set[str] = field(default_factory=set)
    closeBehaviours: list[_CloseBehaviour] = field(default_factory=list)
    closeRequests: list[str] = field(default_factory=list)

    def snapshot(self) -> Any:
        from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
        return NativeLifecycleSnapshot.model_validate({
            "cluster_uuid": "cluster", "nodes": {"node": {"ephemeral_id": "incarnation"}},
            "metadata": {"indices": {name: {"state": "open" if name in self.openNames else "close",
                "settings": {"index": {"uuid": uuid}}} for name, uuid in self.uuids.items()}},
            "routing_table": {"indices": {name: {"shards": {"0": [{"primary": True, "state": "STARTED"}]}}
                for name in self.openNames}},
            "blocks": {"indices": {name: {"4": {"levels": ["read", "write"]}}
                for name in self.uuids if name not in self.openNames}},
        })

    def close(self, name: str) -> None:
        from shared.services.retrieval.lexical.error import LexicalIndexError
        self.closeRequests.append(name)
        behaviour: _CloseBehaviour = self.closeBehaviours.pop(0) if self.closeBehaviours else "normal"
        if behaviour == "unanswered":
            raise LexicalIndexError("Native lexical transport failed", isRetryable=True, hasUnknownOutcome=True)
        self.openNames.discard(name)
        if behaviour == "lostResponse":
            raise LexicalIndexError("Native lexical transport failed", isRetryable=True, hasUnknownOutcome=True)


class FakeIndex:
    def __init__(self, node: FakeNode, indexName: str) -> None:
        self.node: FakeNode = node
        self.indexName: str = indexName

    def getLifecycleSnapshot(self, **arguments: object) -> Any:
        return self.node.snapshot()

    def forScope(self, scope: object) -> FakeIndex:
        return self

    def setIndexOpen(self, *, isOpen: bool, **arguments: object) -> None:
        if isOpen:
            self.node.openNames.add(self.indexName)
            return
        self.node.close(self.indexName)

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class ResolutionRuntime:
    engine: Engine
    node: FakeNode
    sent: list[tuple[str, tuple[object, ...], float]]
    records: list[dict[str, Any]]

    def addIndex(self, namespace: str, *, state: str, isNativeOpen: bool, nativeUuid: str = "uuid") -> tuple[Any, FakeIndex]:
        from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
        from shared.services.retrieval.lexical.scope import LexicalScope
        scope = LexicalScope(ownerId="resolution-user", namespace=namespace)
        indexName: str = f"knowhere-test-{scope.fingerprint[:48]}"
        with Session(self.engine) as db, db.begin():
            db.add(LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId, namespace=namespace,
                corpusKind="private", indexName=indexName, indexUuid="uuid", state=state,
                operationId=None if state in ("open", "closed") else str(uuid4())))
        self.node.uuids[indexName] = nativeUuid
        if isNativeOpen:
            self.node.openNames.add(indexName)
        self.sent.clear()
        return scope, FakeIndex(self.node, indexName)

    def state(self, scope: Any) -> str:
        from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
        with Session(self.engine) as db:
            row: LexicalNamespaceIndex | None = db.get(LexicalNamespaceIndex, scope.fingerprint)
            assert row is not None
            return row.state

    def alerts(self) -> list[dict[str, Any]]:
        return [record for record in self.records
            if record["extra"].get("event") == "lexical.index_lifecycle.unresolved"]


@pytest.fixture
def resolutionRuntime(monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess) -> Iterator[ResolutionRuntime]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.core.celery_app import celery_app
    for taskName in ("maintainLexical", "cleanBuild", "cleanArchivedDocument", "cleanFailedAttempt", "resolveIndex"):
        celery_app.tasks.pop(f"shared.services.retrieval.lexical.maintenance.{taskName}", None)
    from shared.services.retrieval.lexical import resolution
    from shared.services.retrieval.lexical.maintenance_signals import LexicalMaintenanceSignals

    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    node: FakeNode = FakeNode()
    sent: list[tuple[str, tuple[object, ...], float]] = []
    records: list[dict[str, Any]] = []

    def captureTask(name: str, args: tuple[object, ...] = (), countdown: float = 0, **options: object) -> None:
        sent.append((name, tuple(args), countdown))

    def openNative(*, scope: Any, **arguments: object) -> FakeIndex:
        return FakeIndex(node, f"knowhere-test-{scope.fingerprint[:48]}")

    monkeypatch.setattr(celery_app, "send_task", captureTask)
    monkeypatch.setattr(LexicalMaintenanceSignals, "_dispatchPool",
        SimpleNamespace(trySubmit=lambda operation: operation() or True, close=lambda: None))
    monkeypatch.setattr(resolution, "OpenSearchLexicalIndex", openNative)
    sinkId: int = logger.add(lambda message: records.append(dict(message.record)), level="INFO")
    LexicalMaintenanceSignals.install()
    try:
        yield ResolutionRuntime(engine, node, sent, records)
    finally:
        LexicalMaintenanceSignals.uninstall()
        logger.remove(sinkId)
        engine.dispose()


def _coordination(runtime: ResolutionRuntime) -> Any:
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    return LexicalCoordination(sessionmaker(runtime.engine, expire_on_commit=False))


def test_unanswered_close_is_retried_until_the_index_reads_closed(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    scope, native = resolutionRuntime.addIndex("retried", state="open", isNativeOpen=True)
    resolutionRuntime.node.closeBehaviours = ["unanswered", "unanswered"]
    assert _coordination(resolutionRuntime).closeEmpty(scope=scope, native=cast(OpenSearchLexicalIndex, native))
    assert resolutionRuntime.state(scope) == "closed"
    assert len(resolutionRuntime.node.closeRequests) == 3
    assert resolutionRuntime.sent == []


def test_lost_close_response_is_settled_by_reading_the_index(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    scope, native = resolutionRuntime.addIndex("lost-response", state="open", isNativeOpen=True)
    resolutionRuntime.node.closeBehaviours = ["lostResponse"]
    assert _coordination(resolutionRuntime).closeEmpty(scope=scope, native=cast(OpenSearchLexicalIndex, native))
    assert resolutionRuntime.state(scope) == "closed"
    assert len(resolutionRuntime.node.closeRequests) == 1


def test_close_that_never_answers_is_uncertain_and_dispatches_resolution(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.maintenance_signals import RESOLUTION_DELAY_SECONDS, RESOLVE_INDEX_TASK
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    scope, native = resolutionRuntime.addIndex("never-answers", state="open", isNativeOpen=True)
    resolutionRuntime.node.closeBehaviours = ["unanswered"] * 3
    with pytest.raises(LexicalIndexError, match="requires reconciliation"):
        _coordination(resolutionRuntime).closeEmpty(scope=scope, native=cast(OpenSearchLexicalIndex, native))
    assert len(resolutionRuntime.node.closeRequests) == 3
    assert resolutionRuntime.state(scope) == "uncertain"
    assert resolutionRuntime.sent == [(RESOLVE_INDEX_TASK, (scope.fingerprint,), RESOLUTION_DELAY_SECONDS)]
    assert resolutionRuntime.alerts() == []


def test_resolution_records_a_close_that_finished_on_the_node(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    scope, _ = resolutionRuntime.addIndex("finished-late", state="uncertain", isNativeOpen=False)
    assert LexicalIndexResolution().resolveDispatched(scope.fingerprint, 0) == "closed"
    assert resolutionRuntime.state(scope) == "closed"
    assert resolutionRuntime.node.closeRequests == []
    assert resolutionRuntime.sent == []


def test_resolution_closes_an_index_that_is_still_open(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    scope, native = resolutionRuntime.addIndex("still-open", state="uncertain", isNativeOpen=True)
    assert LexicalIndexResolution().resolveDispatched(scope.fingerprint, 0) == "closed"
    assert resolutionRuntime.state(scope) == "closed"
    assert resolutionRuntime.node.closeRequests == [native.indexName]
    assert resolutionRuntime.sent == []


def test_resolution_retries_itself_then_alerts_without_a_second_chain(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.maintenance_signals import RESOLVE_INDEX_TASK
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    scope, _ = resolutionRuntime.addIndex("keeps-failing", state="uncertain", isNativeOpen=True)
    resolutionRuntime.node.closeBehaviours = ["unanswered"] * 9
    resolution = LexicalIndexResolution()
    assert resolution.resolveDispatched(scope.fingerprint, 0) == "rescheduled"
    # Only the self-scheduled retry: the failed attempt's own uncertain commit is suppressed.
    assert resolutionRuntime.sent == [(RESOLVE_INDEX_TASK, (scope.fingerprint, 1), 60)]
    assert resolution.resolveDispatched(scope.fingerprint, 1) == "rescheduled"
    assert resolutionRuntime.alerts() == []
    assert resolution.resolveDispatched(scope.fingerprint, 2) == "failed"
    assert len(resolutionRuntime.sent) == 2
    assert resolutionRuntime.state(scope) == "uncertain"
    alerts: list[dict[str, Any]] = resolutionRuntime.alerts()
    assert len(alerts) == 1 and alerts[0]["level"].name == "ERROR"
    assert alerts[0]["extra"]["registry_ids"] == [scope.fingerprint]


def test_identity_mismatch_is_never_closed_automatically(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    scope, _ = resolutionRuntime.addIndex("replaced", state="uncertain", isNativeOpen=True, nativeUuid="other-uuid")
    assert LexicalIndexResolution().resolveDispatched(scope.fingerprint, 2) == "failed"
    assert resolutionRuntime.state(scope) == "uncertain"
    assert resolutionRuntime.node.closeRequests == []
    assert len(resolutionRuntime.alerts()) == 1


def test_live_reservation_is_left_to_its_issuer(resolutionRuntime: ResolutionRuntime) -> None:
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    scope, _ = resolutionRuntime.addIndex("in-flight", state="closing", isNativeOpen=True)
    assert LexicalIndexResolution().resolveDispatched(scope.fingerprint, 2) == "pending"
    assert resolutionRuntime.state(scope) == "closing"
    assert resolutionRuntime.node.closeRequests == []
    assert resolutionRuntime.alerts() == []


def test_sweep_resolves_every_unsettled_index_and_alerts_once(resolutionRuntime: ResolutionRuntime) -> None:
    import time
    from shared.services.retrieval.lexical.resolution import LexicalIndexResolution

    finished, _ = resolutionRuntime.addIndex("sweep-finished", state="uncertain", isNativeOpen=False)
    stillOpen, _ = resolutionRuntime.addIndex("sweep-open", state="uncertain", isNativeOpen=True)
    replaced, _ = resolutionRuntime.addIndex("sweep-replaced", state="uncertain", isNativeOpen=True, nativeUuid="other")
    healthy, _ = resolutionRuntime.addIndex("sweep-healthy", state="open", isNativeOpen=True)
    assert LexicalIndexResolution().resolveDue(time.monotonic() + 60) == 2
    assert [resolutionRuntime.state(scope) for scope in (finished, stillOpen, replaced, healthy)] == [
        "closed", "closed", "uncertain", "open"]
    assert resolutionRuntime.sent == []
    alerts: list[dict[str, Any]] = resolutionRuntime.alerts()
    assert len(alerts) == 1 and alerts[0]["extra"]["registry_ids"] == [replaced.fingerprint]
