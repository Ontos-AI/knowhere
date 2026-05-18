"""
Run PyMuPDF work in bounded gevent-managed spawned children.

PyMuPDF's C extension is not safe under gevent's cooperative threading.
This module provides a single entry point that:
  1. Schedules work through a shared gevent ThreadPool capped per pod
  2. Each scheduled job still gets its own multiprocessing("spawn") child
  3. Queued time is outside the child timeout budget

Worker contract:
  - Must be a top-level function (picklable by spawn)
  - Decorated with @worker to handle error serialization automatically
  - Signature: (queue: Queue, *args) -> None
  - On success: queue.put({"ok": True, ...payload...})
  - On failure: handled by @worker decorator
"""

import atexit
import functools
import multiprocessing
import queue as queue_module
import time
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue as MultiprocessingQueue
from threading import RLock
from typing import TYPE_CHECKING

from app.core.runtime_limits import read_pymupdf_max_concurrent
from loguru import logger

from shared.core.exceptions.domain_exceptions import (
    PDFParsingException,
    TimeoutException,
)

if TYPE_CHECKING:
    from gevent.threadpool import ThreadPool as GeventThreadPool

# Default timeout for child processes (seconds)
DEFAULT_TIMEOUT = 3000
QUEUE_POLL_INTERVAL_SECONDS = 0.1
CHILD_EXIT_GRACE_SECONDS = 5
POST_RESULT_EXIT_GRACE_SECONDS = 5
POST_KILL_JOIN_GRACE_SECONDS = 1
PROCESS_POOL_SIZE = read_pymupdf_max_concurrent()
PROCESS_POOL_CONTEXT = multiprocessing.get_context("spawn")

_PROCESS_POOL_EXECUTOR: "GeventThreadPool | None" = None
_PROCESS_POOL_LOCK = RLock()


@dataclass(frozen=True)
class _ChildWaitResult:
    status: str
    result: dict | None = None
    child_pid: int | None = None
    received_at: float | None = None


@dataclass(frozen=True)
class _ThreadPoolTaskResult:
    result: dict | None = None
    error: Exception | None = None


def _wait_for_child_result(
    proc: BaseProcess,
    result_queue: MultiprocessingQueue,
    timeout: int,
) -> _ChildWaitResult:
    """Read the child queue before join() so large payloads cannot self-timeout."""
    deadline = time.monotonic() + timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _ChildWaitResult(status="timeout")

        try:
            result = result_queue.get(
                timeout=min(remaining, QUEUE_POLL_INTERVAL_SECONDS)
            )
            return _ChildWaitResult(
                status="result",
                result=result,
                received_at=time.monotonic(),
            )
        except queue_module.Empty:
            if not proc.is_alive():
                return _ChildWaitResult(status="crash")


def _get_process_pool_executor() -> "GeventThreadPool":
    global _PROCESS_POOL_EXECUTOR
    with _PROCESS_POOL_LOCK:
        if _PROCESS_POOL_EXECUTOR is None:
            from gevent.threadpool import ThreadPool

            _PROCESS_POOL_EXECUTOR = ThreadPool(maxsize=PROCESS_POOL_SIZE)
            logger.info(
                f"[pymupdf-subprocess] initialized gevent thread pool size={PROCESS_POOL_SIZE}"
            )
        return _PROCESS_POOL_EXECUTOR


def _shutdown_process_pool() -> None:
    global _PROCESS_POOL_EXECUTOR
    with _PROCESS_POOL_LOCK:
        executor = _PROCESS_POOL_EXECUTOR
        _PROCESS_POOL_EXECUTOR = None

    if executor is None:
        return

    executor.kill()
    executor.join()


def _close_result_queue(result_queue: MultiprocessingQueue) -> None:
    """Release parent-side queue resources once the child result is no longer needed."""
    try:
        result_queue.close()
    except Exception as exc:
        logger.debug(f"Failed to close PyMuPDF result queue: {exc}")

    try:
        result_queue.join_thread()
    except Exception as exc:
        logger.debug(f"Failed to join PyMuPDF result queue thread: {exc}")


def _run_worker_in_spawned_process(
    worker_fn,
    args: tuple,
    timeout: int,
) -> dict:
    """Spawn one isolated child process, but only after a pooled slot is available."""
    ctx = PROCESS_POOL_CONTEXT
    result_queue = ctx.Queue()
    proc = ctx.Process(target=worker_fn, args=(result_queue, *args))

    t0 = time.monotonic()
    proc.start()
    child_pid = proc.pid
    logger.info(f"[pymupdf-subprocess] started pid={child_pid} fn={worker_fn.__name__}")

    wait_result = _wait_for_child_result(proc, result_queue, timeout)

    if wait_result.status == "timeout" and not proc.is_alive():
        wait_result = _ChildWaitResult(status="crash")

    if wait_result.status == "timeout":
        proc.kill()
        proc.join(timeout=CHILD_EXIT_GRACE_SECONDS)
        _close_result_queue(result_queue)
        elapsed = time.monotonic() - t0
        logger.error(
            f"[pymupdf-subprocess] TIMEOUT pid={child_pid} fn={worker_fn.__name__} "
            f"after {timeout}s elapsed={elapsed:.1f}s — child killed"
        )
        raise TimeoutException(
            internal_message=(
                f"pymupdf child timed out after {timeout}s: "
                f"fn={worker_fn.__name__}, pid={child_pid}"
            ),
            retry_after=30,
        )

    if wait_result.status == "crash":
        proc.join(timeout=CHILD_EXIT_GRACE_SECONDS)
        elapsed = time.monotonic() - t0
        _close_result_queue(result_queue)
        logger.error(
            f"[pymupdf-subprocess] CRASH pid={child_pid} fn={worker_fn.__name__} "
            f"exitcode={proc.exitcode} elapsed={elapsed:.1f}s — no result on queue"
        )
        raise PDFParsingException(
            user_message="Failed to process your document. Please try again.",
            reason="SUBPROCESS_CRASH",
            internal_message=(
                f"pymupdf child exited with code={proc.exitcode} and no result: "
                f"fn={worker_fn.__name__}, pid={child_pid}"
            ),
        )

    _close_result_queue(result_queue)
    proc.join(timeout=POST_RESULT_EXIT_GRACE_SECONDS)
    elapsed = time.monotonic() - t0
    exit_lag = None
    if wait_result.received_at is not None:
        exit_lag = elapsed - (wait_result.received_at - t0)

    if proc.is_alive():
        proc.kill()
        proc.join(timeout=POST_KILL_JOIN_GRACE_SECONDS)
        elapsed = time.monotonic() - t0
        if wait_result.received_at is not None:
            exit_lag = elapsed - (wait_result.received_at - t0)
        logger.warning(
            f"[pymupdf-subprocess] EXIT_DELAY pid={child_pid} fn={worker_fn.__name__} "
            f"elapsed={elapsed:.1f}s exit_lag={exit_lag:.1f}s "
            f"— result returned before child exited; child killed after grace"
        )

    result = wait_result.result or {}
    if not result.get("ok"):
        logger.error(
            f"[pymupdf-subprocess] FAILED pid={child_pid} fn={worker_fn.__name__} "
            f"elapsed={elapsed:.1f}s — {result.get('error_type')}: {result.get('error_msg')}"
        )
        raise PDFParsingException(
            user_message="Failed to process your document. Please try again.",
            reason="SUBPROCESS_FAILED",
            internal_message=(
                f"pymupdf child failed: {result.get('error_type')}: "
                f"{result.get('error_msg')}"
            ),
        )

    logger.info(
        f"[pymupdf-subprocess] done pid={child_pid} fn={worker_fn.__name__} "
        f"elapsed={elapsed:.1f}s"
    )
    return result


def _run_worker_in_spawned_process_safe(
    worker_fn,
    args: tuple,
    timeout: int,
) -> _ThreadPoolTaskResult:
    """Return exceptions to the caller greenlet so gevent does not log them as thread failures."""
    try:
        return _ThreadPoolTaskResult(
            result=_run_worker_in_spawned_process(worker_fn, args, timeout)
        )
    except Exception as exc:
        return _ThreadPoolTaskResult(error=exc)


atexit.register(_shutdown_process_pool)


def worker(fn):
    """Decorator that wraps a child-process worker with error handling.

    The decorated function still receives (queue, *args). On unhandled
    exception, the error is serialized to the queue as a plain dict
    so the parent can raise a clean RuntimeError.
    """

    @functools.wraps(fn)
    def wrapped(queue, *args):
        try:
            fn(queue, *args)
        except Exception as exc:
            # Log in child before serializing — child stderr may be the only
            # trace if the parent loses the queue result.
            import traceback

            traceback.print_exc()
            queue.put(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc),
                }
            )

    return wrapped


def run_in_child_process(
    worker_fn,
    *args,
    timeout: int = DEFAULT_TIMEOUT,
):
    """Run PyMuPDF work through a bounded gevent threadpool slot.

    Args:
        worker_fn: Top-level function with signature (queue, *args) -> None.
        *args: Arguments forwarded to worker_fn after the queue.
        timeout: Max seconds to wait before killing the child.

    Returns:
        dict with at least {"ok": True, ...} from the worker.

    Raises:
        TimeoutError: Child did not finish within timeout.
        RuntimeError: Child exited abnormally or reported failure.
    """
    logger.info(
        f"[pymupdf-subprocess] queued fn={worker_fn.__name__} "
        f"pool_size={PROCESS_POOL_SIZE}"
    )

    executor = _get_process_pool_executor()
    task_result = executor.apply(
        _run_worker_in_spawned_process_safe,
        (worker_fn, args, timeout),
    )
    if task_result.error is not None:
        raise task_result.error
    return task_result.result or {}
