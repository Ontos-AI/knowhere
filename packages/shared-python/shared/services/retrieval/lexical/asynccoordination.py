"""Await lifecycle I/O around the existing short SQL admission decisions."""

import asyncio
from functools import partial
import time

from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.coordination import LexicalCoordination, _CapacityWait, _Observation, _Operation, _WAIT_SECONDS
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.core.config import settings
from shared.services.retrieval.lexical.stage_observer import StageObserver


class AsyncLexicalCoordination:
    def __init__(self, database: AsyncLexicalDatabase, *, executionDeadline: IndexingDeadline | None = None) -> None:
        self.database: AsyncLexicalDatabase = database
        self.decisions: LexicalCoordination = LexicalCoordination(database.sessions)
        self._executionDeadline: IndexingDeadline | None = executionDeadline

    async def ensureIndex(self, *, scope: LexicalScope,
                          native: AsyncOpenSearchLexicalIndex) -> tuple[str, int]:
        deadline: float = time.monotonic() + _WAIT_SECONDS
        observation: _Observation = await self.database.run(partial(self.decisions._beginObservation, deadline))
        exact: NativeLifecycleSnapshot = await native.getLifecycleSnapshot(
            isExact=True, timeoutSeconds=self.decisions._remaining(deadline))
        observed: tuple[str, int] | None = await self.database.run(partial(self.decisions.observeIndex,
            scope=scope, indexName=native.indexName, observation=observation, exact=exact, deadline=deadline))
        if observed is not None:
            return observed
        while True:
            observation = await self.database.run(partial(self.decisions._beginObservation, deadline))
            inventory: NativeLifecycleSnapshot = await native.getLifecycleSnapshot(
                isExact=False, timeoutSeconds=self.decisions._remaining(deadline))
            decision: _Operation | tuple[str, int] | LexicalIndexError | _CapacityWait | None = await self.database.run(
                partial(self.decisions._allocate, scope, native.indexName, observation, inventory, deadline=deadline))
            if isinstance(decision, LexicalIndexError):
                raise decision
            if isinstance(decision, tuple):
                return decision
            if decision is None:
                await asyncio.sleep(min(0.05, self.decisions._remaining(deadline)))
                continue
            if isinstance(decision, _CapacityWait):
                with StageObserver.measure("capacity_wait_ms"):
                    await asyncio.sleep(min(0.05, self.decisions._remaining(deadline)))
                continue
            operationDeadline: float = deadline if decision.action == "close" else time.monotonic() + _WAIT_SECONDS
            if decision.action == "close":
                with StageObserver.measure("capacity_wait_ms", intervalName="lru_intervals_utc"):
                    identity: str = await self._execute(native, decision, deadline=operationDeadline)
            else:
                identity = await self._execute(native, decision, deadline=operationDeadline)
            if decision.action != "close":
                return identity, decision.epoch

    async def _execute(self, native: AsyncOpenSearchLexicalIndex, operation: _Operation,
                       *, deadline: float) -> str:
        target: AsyncOpenSearchLexicalIndex = native.forScope(operation.scope)
        try:
            if target.indexName != operation.indexName:
                raise LexicalIndexError("Native lifecycle target differs from its registry")
            before: NativeLifecycleSnapshot = await target.getLifecycleSnapshot(
                isExact=True, timeoutSeconds=self.decisions._remaining(deadline))
            self.decisions._checkNativeIdentity(before, operation)
            if before.getIndexUuid(target.indexName) != operation.indexUuid:
                raise LexicalIndexError("Native lifecycle target identity changed")
            if operation.action == "create":
                await target.createIndex(operationId=operation.identity,
                    timeoutSeconds=self.decisions._remaining(deadline))
            else:
                if (operation.action == "close" and not before.isOpen(target.indexName)
                    or operation.action == "open" and not before.isClosed(target.indexName)):
                    raise LexicalIndexError("Native lifecycle target state changed")
                await target.setIndexOpen(isOpen=operation.action == "open", operationId=operation.identity,
                    timeoutSeconds=self.decisions._remaining(deadline))
            after: NativeLifecycleSnapshot = await target.getLifecycleSnapshot(
                isExact=True, timeoutSeconds=self.decisions._remaining(deadline))
            self.decisions._checkNativeIdentity(after, operation)
            identity: str | None = after.getIndexUuid(target.indexName)
            if (not identity or operation.indexUuid not in (None, identity)
                or not (after.isClosed(target.indexName) if operation.action == "close" else after.isReady(target.indexName))):
                raise LexicalIndexError("Native lifecycle completion could not be verified")
            await self.database.run(partial(self.decisions._finish, operation, identity, deadline=deadline))
            return identity
        except BaseException as error:
            isTimeout: bool = isinstance(error, TimeoutError) or (
                isinstance(error, LexicalIndexError) and error.isTimeout
            )
            try:
                settlement: IndexingDeadline = (self._executionDeadline.startSettlement(
                    settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS) if self._executionDeadline
                    else IndexingDeadline(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS))
                async with asyncio.timeout(settlement.getRemainingSeconds()):
                    await self.database.run(partial(self.decisions._finish, operation, None,
                        deadline=time.monotonic() + settlement.getRemainingSeconds()))
            except Exception as settlementError:
                # The committed opening/closing reservation remains charged if
                # uncertainty cannot be journaled. Never return native success.
                if isinstance(error, asyncio.CancelledError):
                    raise error from settlementError
                raise LexicalIndexError("Native lifecycle settlement requires reconciliation",
                    isRetryable=True, hasUnknownOutcome=True, isTimeout=isTimeout) from settlementError
            if isinstance(error, asyncio.CancelledError):
                raise
            raise LexicalIndexError("Native index operation requires reconciliation",
                isRetryable=True, hasUnknownOutcome=True, isTimeout=isTimeout) from error
        finally:
            target.close()
