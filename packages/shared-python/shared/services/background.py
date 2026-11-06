"""Bounded best-effort work whose durable intent lives in the database."""

from _thread import LockType
import atexit
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast

from gevent.monkey import get_original, is_module_patched
from gevent.threadpool import ThreadPool
from loguru import logger


class BackgroundWorkPool:
    def __init__(self, *, name: str, maximumWorkers: int = 2) -> None:
        if maximumWorkers <= 0:
            raise ValueError("Background worker count must be positive")
        createLock: Callable[[], LockType] = cast(Callable[[], LockType], get_original("_thread", "allocate_lock"))
        self._lock: LockType = createLock()
        self._name: str = name
        self._maximumWorkers: int = maximumWorkers
        self._inflight: int = 0
        self._isClosed: bool = False
        # Gevent owns native-thread hubs and their teardown. Raw _thread workers
        # leave cooperative I/O alive during interpreter finalization. The API's
        # unpatched runtime uses the standard native executor instead.
        self._geventPool: ThreadPool | None = (
            ThreadPool(maximumWorkers, idle_task_timeout=1)
            if is_module_patched("threading") else None)
        self._executor: ThreadPoolExecutor | None = (
            None if self._geventPool is not None else
            ThreadPoolExecutor(max_workers=maximumWorkers, thread_name_prefix=name))
        if self._geventPool is not None:
            # Closing admission stays non-blocking. Natural interpreter exit
            # must finish native work before it destroys greenlet's C runtime,
            # matching the standard executor's interpreter-shutdown behavior.
            atexit.register(self._geventPool.kill)

    def trySubmit(self, operation: Callable[[], None]) -> bool:
        # Admit only immediately available execution slots. Saturation leaves
        # database intent pending instead of building an unbounded memory queue.
        with self._lock:
            if self._isClosed or self._inflight >= self._maximumWorkers:
                return False
            self._inflight += 1
        try:
            if self._geventPool is not None:
                self._geventPool.spawn(self._execute, operation)
            else:
                assert self._executor is not None
                self._executor.submit(self._execute, operation)
            return True
        except (RuntimeError, OSError):
            with self._lock:
                self._inflight -= 1
            return False

    def close(self) -> None:
        with self._lock:
            self._isClosed = True
        if self._geventPool is not None:
            # Unlike kill()/size=0 or the gevent executor's shutdown(), reducing
            # maxsize retires workers cooperatively without joining active I/O.
            self._geventPool.maxsize = 0
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _execute(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception as error:
            logger.error("Background work remains recoverable: pool={}, error_type={}", self._name, type(error).__name__)
        finally:
            with self._lock:
                self._inflight -= 1
