"""Verify namespace turnover using an explicitly isolated native test cluster."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Event
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)


def _fillReservations(engine: Engine, prefix: str, count: int) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex

    with Session(engine) as database, database.begin():
        database.add_all(
            [
                LexicalNamespaceIndex(
                    id=f"{position:064x}",
                    ownerId="unresolved",
                    namespace=f"unresolved-{position}",
                    corpusKind="private",
                    indexName=f"{prefix}-unresolved-{position}",
                    state="uncertain",
                )
                for position in range(count)
            ]
        )


@pytest.fixture
def turnoverRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess
) -> Iterator[tuple[Engine, str, str]]:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("An isolated OpenSearch endpoint is required")
    prefix: str = f"knowhere-turnover-{uuid4().hex[:10]}"
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_INDEX_PREFIX", prefix)
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        yield engine, endpoint, prefix
    finally:
        engine.dispose()


def test_capacity_evicts_oldest_idle_index_and_reopens_its_data(
    turnoverRuntime: tuple[Engine, str, str],
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit

    engine, endpoint, prefix = turnoverRuntime
    scopes: tuple[LexicalScope, ...] = tuple(
        LexicalScope(ownerId="turnover-contract", namespace=name)
        for name in ("old", "recent", "cold")
    )
    indexes: list[OpenSearchLexicalIndex] = [
        OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
        for scope in scopes
    ]
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    try:
        _fillReservations(engine, prefix, 398)
        firstUuid, _ = coordinator.ensureIndex(scope=scopes[0], native=indexes[0])
        indexes[0].writeBuild(
            buildId="published",
            units=(
                LexicalUnit(
                    documentId="document",
                    revisionId="revision",
                    unitId="unit",
                    sectionId="section",
                    contentTokens="test term",
                ),
            ),
        )
        coordinator.ensureIndex(scope=scopes[1], native=indexes[1])
        coordinator.ensureIndex(scope=scopes[2], native=indexes[2])
        with Session(engine) as database:
            assert (
                database.scalars(
                    select(LexicalNamespaceIndex.state).where(
                        LexicalNamespaceIndex.id == scopes[0].fingerprint
                    )
                ).one()
                == "closed"
            )
            assert (
                database.scalars(
                    select(func.count())
                    .select_from(LexicalNamespaceIndex)
                    .where(LexicalNamespaceIndex.state != "closed")
                ).one()
                == 400
            )
        assert (
            coordinator.ensureIndex(scope=scopes[0], native=indexes[0])[0] == firstUuid
        )
        assert indexes[0].search(
            buildIds=("published",), queryTokens=("term",), limit=1
        )
    finally:
        for index in indexes:
            httpx.delete(f"{endpoint}/{index.indexName}", timeout=5, trust_env=False)
            index.close()


def test_same_cold_index_waiters_share_one_eviction(
    turnoverRuntime: tuple[Engine, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    scopes: tuple[LexicalScope, ...] = tuple(
        LexicalScope(ownerId="turnover-contract", namespace=name)
        for name in ("old", "recent", "cold")
    )
    indexes: list[OpenSearchLexicalIndex] = [
        OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
        for scope in scopes
    ]
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    firstClose: Event = Event()
    secondClose: Event = Event()
    release: Event = Event()
    secondObserved: Event = Event()
    setOpen = OpenSearchLexicalIndex.setIndexOpen
    getSnapshot = OpenSearchLexicalIndex.getLifecycleSnapshot

    def pauseClose(
        native: OpenSearchLexicalIndex,
        *,
        isOpen: bool,
        operationId: str,
        timeoutSeconds: float,
    ) -> None:
        if not isOpen and native.indexName == indexes[0].indexName:
            firstClose.set()
            assert release.wait(3)
        elif not isOpen:
            secondClose.set()
        setOpen(
            native,
            isOpen=isOpen,
            operationId=operationId,
            timeoutSeconds=timeoutSeconds,
        )

    def observe(
        native: OpenSearchLexicalIndex, *, isExact: bool, timeoutSeconds: float
    ):
        snapshot = getSnapshot(native, isExact=isExact, timeoutSeconds=timeoutSeconds)
        if (
            native.indexName == indexes[2].indexName
            and not isExact
            and firstClose.is_set()
        ):
            secondObserved.set()
        return snapshot

    try:
        _fillReservations(engine, prefix, 398)
        coordinator.ensureIndex(scope=scopes[0], native=indexes[0])
        coordinator.ensureIndex(scope=scopes[1], native=indexes[1])
        monkeypatch.setattr(OpenSearchLexicalIndex, "setIndexOpen", pauseClose)
        monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", observe)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                coordinator.ensureIndex, scope=scopes[2], native=indexes[2]
            )
            try:
                assert firstClose.wait(2)
                second = executor.submit(
                    coordinator.ensureIndex, scope=scopes[2], native=indexes[2]
                )
                assert secondObserved.wait(2)
                secondClose.wait(0.5)
            finally:
                release.set()
            assert first.result(timeout=5) == second.result(timeout=5)
        with Session(engine) as database:
            assert (
                database.scalars(
                    select(LexicalNamespaceIndex.state).where(
                        LexicalNamespaceIndex.id == scopes[1].fingerprint
                    )
                ).one()
                == "open"
            )
    finally:
        for index in indexes:
            httpx.delete(f"{endpoint}/{index.indexName}", timeout=5, trust_env=False)
            index.close()


def test_lost_close_acknowledgment_cannot_release_slot_or_reopen(
    turnoverRuntime: tuple[Engine, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    scope = LexicalScope(ownerId="turnover-contract", namespace="old")
    coldScope = LexicalScope(ownerId="turnover-contract", namespace="cold")
    index = OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
    cold = OpenSearchLexicalIndex(
        endpoint=endpoint, scope=coldScope, indexPrefix=prefix
    )
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    setOpen = OpenSearchLexicalIndex.setIndexOpen

    def loseResponse(
        native: OpenSearchLexicalIndex,
        *,
        isOpen: bool,
        operationId: str,
        timeoutSeconds: float,
    ) -> None:
        setOpen(
            native,
            isOpen=isOpen,
            operationId=operationId,
            timeoutSeconds=timeoutSeconds,
        )
        raise LexicalIndexError(
            "Injected lost lifecycle response", hasUnknownOutcome=True
        )

    try:
        _fillReservations(engine, prefix, 399)
        coordinator.ensureIndex(scope=scope, native=index)
        monkeypatch.setattr(OpenSearchLexicalIndex, "setIndexOpen", loseResponse)
        with pytest.raises(LexicalIndexError, match="requires reconciliation"):
            coordinator.ensureIndex(scope=coldScope, native=cold)
        assert index.getLifecycleSnapshot(isExact=True, timeoutSeconds=3).isClosed(
            index.indexName
        )
        with pytest.raises(LexicalIndexError, match="requires reconciliation"):
            coordinator.ensureIndex(scope=scope, native=index)
        with Session(engine) as database:
            row = database.get(LexicalNamespaceIndex, scope.fingerprint)
            assert row is not None and row.state == "uncertain" and row.operationId
            assert database.get(LexicalNamespaceIndex, coldScope.fingerprint) is None
    finally:
        for target in (index, cold):
            httpx.delete(f"{endpoint}/{target.indexName}", timeout=5, trust_env=False)
            target.close()


def test_periodic_turnover_does_not_treat_native_use_as_a_close_fence(
    turnoverRuntime: tuple[Engine, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.models.database.lexicallease import LexicalLease
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical import turnover
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    scopes: tuple[LexicalScope, ...] = tuple(
        LexicalScope(ownerId="turnover-contract", namespace=name)
        for name in ("busy", "first", "second")
    )
    indexes: list[OpenSearchLexicalIndex] = [
        OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
        for scope in scopes
    ]
    factory = sessionmaker(engine, expire_on_commit=False)
    coordinator = LexicalCoordination(factory)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK", 3)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK", 1)
    try:
        for scope, index in zip(scopes, indexes, strict=True):
            coordinator.ensureIndex(scope=scope, native=index)
        with factory.begin() as database:
            database.add(
                LexicalLease(
                    namespaceIndexId=scopes[0].fingerprint,
                    kind="index_use",
                    ownerToken="old-owner",
                    backendEpoch=1,
                    expiresAt=coordinator.getDatabaseTime(database)
                    - timedelta(seconds=30),
                )
            )
        monkeypatch.setattr(turnover, "get_sync_session_factory", lambda: factory)
        task = turnover.turnOverLexicalIndexes
        assert task.run() == 2
        with Session(engine) as database:
            assert database.scalars(
                select(LexicalNamespaceIndex.namespace).where(
                    LexicalNamespaceIndex.state == "open"
                )
            ).all() == ["second"]
    finally:
        for index in indexes:
            httpx.delete(f"{endpoint}/{index.indexName}", timeout=5, trust_env=False)
            index.close()


def test_unregistered_business_index_counts_at_capacity(
    turnoverRuntime: tuple[Engine, str, str],
) -> None:
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    otherScope = LexicalScope(ownerId="turnover-contract", namespace="unregistered")
    scope = LexicalScope(ownerId="turnover-contract", namespace="waiting")
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
    unregistered = native.forScope(otherScope)
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    try:
        _fillReservations(engine, prefix, 399)
        unregistered.createIndex()
        started: float = time.monotonic()
        with pytest.raises(LexicalIndexError, match="admission timed out") as failure:
            coordinator.ensureIndex(scope=scope, native=native)
        assert failure.value.isRetryable
        assert time.monotonic() - started < 6
        inventory = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=3)
        assert inventory.isReady(unregistered.indexName)
        assert inventory.getIndexUuid(native.indexName) is None
    finally:
        for index in (unregistered, native):
            httpx.delete(f"{endpoint}/{index.indexName}", timeout=5, trust_env=False)
            index.close()


def test_new_node_incarnation_reconciles_pending_close_before_serving(
    turnoverRuntime: tuple[Engine, str, str],
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    first = LexicalScope(ownerId="turnover-contract", namespace="pending-close")
    second = LexicalScope(ownerId="turnover-contract", namespace="healthy")
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=first, indexPrefix=prefix)
    healthy = native.forScope(second)
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    try:
        coordinator.ensureIndex(scope=first, native=native)
        identity, epoch = coordinator.ensureIndex(scope=second, native=healthy)
        with Session(engine) as database, database.begin():
            control = database.get(LexicalBackendControl, 1)
            row = database.get(LexicalNamespaceIndex, first.fingerprint)
            assert control is not None and row is not None
            control.nodeIncarnation = "previous-node-incarnation"
            row.state = "closing"
            row.operationId = str(uuid4())
        assert coordinator.ensureIndex(scope=second, native=healthy) == (
            identity,
            epoch,
        )
        with pytest.raises(LexicalIndexError, match="requires reconciliation"):
            coordinator.ensureIndex(scope=first, native=native)
        assert native.getLifecycleSnapshot(isExact=True, timeoutSeconds=3).isReady(
            native.indexName
        )
    finally:
        for index in (native, healthy):
            httpx.delete(f"{endpoint}/{index.indexName}", timeout=5, trust_env=False)
            index.close()


def test_full_capacity_uses_real_native_indexes(
    turnoverRuntime: tuple[Engine, str, str],
) -> None:
    if os.environ.get("KNOWHERE_TEST_OPENSEARCH_FULL_CAPACITY") != "1":
        pytest.skip(
            "Full capacity verification needs an explicitly enabled isolated node"
        )
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    scopes: list[LexicalScope] = [
        LexicalScope(ownerId="turnover-capacity", namespace=str(position))
        for position in range(401)
    ]
    native = OpenSearchLexicalIndex(
        endpoint=endpoint, scope=scopes[0], indexPrefix=prefix
    )
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    names: list[str] = []
    identities: dict[int, str] = {}

    def admit(position: int) -> None:
        index: OpenSearchLexicalIndex = native.forScope(scopes[position])
        try:
            if position not in identities:
                names.append(index.indexName)
            identities[position] = coordinator.ensureIndex(
                scope=scopes[position], native=index
            )[0]
        finally:
            index.close()

    def verifyCounts(*, openCount: int, closedCount: int) -> None:
        inventory = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=5)
        assert sum(inventory.isOpen(name) for name in names) == openCount
        assert sum(inventory.isClosed(name) for name in names) == closedCount
        print(
            f"verified native business indexes: open={openCount} closed={closedCount}",
            flush=True,
        )

    try:
        for position in range(360):
            admit(position)
        verifyCounts(openCount=360, closedCount=0)
        assert coordinator.trimIdle(native) == 40
        verifyCounts(openCount=320, closedCount=40)
        original: str = identities[0]
        for position in (*range(40), *range(360, 400)):
            admit(position)
        assert identities[0] == original
        verifyCounts(openCount=400, closedCount=0)
        admit(400)
        verifyCounts(openCount=400, closedCount=1)
        inventory = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=5)
        assert inventory.isClosed(names[40])
    finally:
        with httpx.Client(base_url=endpoint, timeout=15, trust_env=False) as client:
            for offset in range(0, len(names), 20):
                response = client.delete("/" + ",".join(names[offset : offset + 20]))
                assert response.status_code in (200, 404)
        native.close()


def test_index_admission_wait_is_bounded_when_sql_gate_is_locked(
    turnoverRuntime: tuple[Engine, str, str],
) -> None:
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, endpoint, prefix = turnoverRuntime
    scope = LexicalScope(ownerId="turnover-contract", namespace="waiting")
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix=prefix)
    coordinator = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    try:
        with Session(engine) as database, database.begin():
            coordinator.lockControl(database)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with Session(engine) as database, database.begin():
                coordinator.lockControl(database, isExclusive=True)
                waiter = executor.submit(
                    coordinator.ensureIndex, scope=scope, native=native
                )
                with pytest.raises(
                    LexicalIndexError, match="admission timed out"
                ) as failure:
                    waiter.result(timeout=6)
                assert failure.value.isRetryable
    finally:
        httpx.delete(f"{endpoint}/{native.indexName}", timeout=5, trust_env=False)
        native.close()
