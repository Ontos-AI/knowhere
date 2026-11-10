"""Install campaign-only observers at the pinned pools' actual waiting seams."""

from collections.abc import Callable
import inspect
import time
from typing import cast

from httpcore._async.connection_pool import AsyncPoolRequest
from httpcore._async.interfaces import AsyncConnectionInterface
from httpcore._sync.connection_pool import PoolRequest
from httpcore._sync.interfaces import ConnectionInterface
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.util.queue import AsyncAdaptedQueue, Queue

from shared.services.retrieval.lexical.stage_observer import StageObserver


def _getObserver() -> type[StageObserver]:
    return cast(type[StageObserver], getattr(PoolRequest, "_knowhere_stage_observer_class", StageObserver))


def installPoolObservers(observer: type[StageObserver]) -> None:
    setattr(PoolRequest, "_knowhere_stage_observer_class", observer)
    if getattr(PoolRequest.wait_for_connection, "_knowhere_stage_observer", False):
        return
    if (tuple(inspect.signature(PoolRequest.wait_for_connection).parameters) != ("self", "timeout")
        or tuple(inspect.signature(AsyncPoolRequest.wait_for_connection).parameters) != ("self", "timeout")
        or tuple(inspect.signature(Queue.get).parameters) != ("self", "block", "timeout")
        or tuple(inspect.signature(AsyncAdaptedQueue.get).parameters) != ("self", "block", "timeout")):
        raise ValueError("The installed pool API differs from the source-qualified observer seams")
    originalSync = PoolRequest.wait_for_connection
    originalAsync = AsyncPoolRequest.wait_for_connection

    def waitSync(self: PoolRequest, timeout: float | None = None) -> ConnectionInterface:
        active = _getObserver()
        if not active.isNativeRequest():
            return originalSync(self, timeout)
        with active.measure("native_http_pool_wait_ms"):
            return originalSync(self, timeout)

    async def waitAsync(self: AsyncPoolRequest, timeout: float | None = None) -> AsyncConnectionInterface:
        active = _getObserver()
        if not active.isNativeRequest():
            return await originalAsync(self, timeout)
        with active.measure("native_http_pool_wait_ms"):
            return await originalAsync(self, timeout)

    for queue in (Queue, AsyncAdaptedQueue):
        originalGet: Callable[..., object] = queue.get

        def createGet(original: Callable[..., object]) -> Callable[..., object]:
            def get(instance: object, block: bool = True, timeout: float | None = None) -> object:
                if not block:
                    return original(instance, block=block, timeout=timeout)
                with _getObserver().measure("pg_pool_wait_ms"):
                    return original(instance, block=block, timeout=timeout)
            return get

        setattr(queue, "get", createGet(originalGet))
    setattr(waitSync, "_knowhere_stage_observer", True)
    PoolRequest.wait_for_connection = waitSync
    AsyncPoolRequest.wait_for_connection = waitAsync

    def beginTransaction(connection: Connection) -> None:
        if _getObserver().hasEvent():
            connection.info["knowhere_stage_transaction_start"] = time.perf_counter()

    def finishTransaction(connection: Connection) -> None:
        started: object = connection.info.pop("knowhere_stage_transaction_start", None)
        if isinstance(started, float):
            _getObserver().add("pg_transaction_ms", (time.perf_counter() - started) * 1000)

    event.listen(Engine, "begin", cast(Callable[..., object], beginTransaction))
    event.listen(Engine, "commit", cast(Callable[..., object], finishTransaction))
    event.listen(Engine, "rollback", cast(Callable[..., object], finishTransaction))
