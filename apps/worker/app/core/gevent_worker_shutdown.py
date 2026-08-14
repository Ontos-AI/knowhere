"""Bound normal Celery warm shutdown for the gevent worker pool."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import gevent
from celery.concurrency.gevent import TaskPool as GeventTaskPool
from celery.worker import WorkController, state
from celery.worker.request import Request
from gevent import Greenlet
from loguru import logger

_TerminateJob = Callable[[GeventTaskPool, int, int | None], None]


class _ShutdownCancellationPool:
    """Expose Celery's pool cancellation interface only for shutdown."""

    def __init__(
        self,
        pool: GeventTaskPool,
        terminate_job: _TerminateJob,
    ) -> None:
        self._pool: GeventTaskPool = pool
        self._terminate_job: _TerminateJob = terminate_job

    def terminate_job(self, pid: int, signal: int | None = None) -> None:
        self._terminate_job(self._pool, pid, signal)


def _ignore_reconnect_cancellation(
    self: GeventTaskPool,
    pid: int,
    signal: int | None = None,
) -> None:
    """Keep the existing broker-reconnect behavior for gevent tasks."""
    logger.warning(
        "Ignoring gevent task cancellation outside bounded worker shutdown "
        f"(pid={pid}); relying on RedisJobLock for redelivery deduplication"
    )


class GeventWorkerShutdownController:
    """Cancel unacknowledged active tasks after a bounded warm shutdown.

    Celery's ``worker_soft_shutdown_timeout`` does not bound normal SIGTERM.
    Celery applies that setting only after entering its cold/SIGQUIT path. That
    path is unsafe for this worker because Celery calls its patched ``sleep``
    directly from the gevent signal callback, which raises
    ``BlockingSwitchOutError``. This controller keeps normal warm SIGTERM and
    schedules its own non-blocking timer instead.

    The worker also intentionally ignores Celery cancellation requests caused
    by broker reconnects. The original gevent cancellation method is retained
    privately and exposed only to this explicit shutdown path, so reconnects
    cannot accidentally kill useful work while Redis redelivery is settling.
    """

    def __init__(
        self,
        worker: WorkController,
        timeout_seconds: float,
    ) -> None:
        self._worker: WorkController = worker
        self._timeout_seconds: float = timeout_seconds
        self._shutdown_timer: Greenlet | None = None
        self._has_scheduled_shutdown: bool = False
        self._original_terminate_job: _TerminateJob = (
            self._patch_reconnect_cancellation()
        )

    def schedule(self) -> None:
        """Schedule the one bounded cancellation pass for warm SIGTERM."""
        if self._has_scheduled_shutdown:
            logger.info("Bounded worker shutdown is already scheduled")
            return

        self._has_scheduled_shutdown = True

        # ``worker_shutting_down`` is emitted inside Celery's signal handler.
        # Waiting or killing a greenlet there would try to switch out of the
        # gevent hub callback and reproduce the staging BlockingSwitchOutError.
        # ``spawn_later`` only arms a timer here; its callback runs in a normal
        # greenlet where cooperative cancellation is safe.
        self._shutdown_timer = gevent.spawn_later(
            self._timeout_seconds,
            self._cancel_unacknowledged_active_tasks,
        )
        logger.warning(
            "Scheduled bounded warm shutdown cancellation in "
            f"{self._timeout_seconds:g} seconds"
        )

    def close(self) -> None:
        """Disarm a pending timer after the worker finishes naturally."""
        shutdown_timer: Greenlet | None = self._shutdown_timer
        if shutdown_timer is not None and not shutdown_timer.dead:
            shutdown_timer.kill(block=False)
        self._shutdown_timer = None

    @staticmethod
    def _patch_reconnect_cancellation() -> _TerminateJob:
        original_terminate_job: _TerminateJob = cast(
            _TerminateJob,
            getattr(
                GeventTaskPool,
                "_original_terminate_job",
                GeventTaskPool.terminate_job,
            ),
        )

        if not hasattr(GeventTaskPool, "_original_terminate_job"):
            setattr(
                GeventTaskPool,
                "_original_terminate_job",
                original_terminate_job,
            )

        # Celery uses ``pool.terminate_job`` both for broker reconnect recovery
        # and for deliberate task cancellation. Replacing the class method
        # only for a moment would race with reconnect handling, so the public
        # pool behavior stays a no-op and shutdown uses the private adapter
        # above to reach the saved original method.
        GeventTaskPool.terminate_job = _ignore_reconnect_cancellation
        logger.info(
            "Patched gevent TaskPool.terminate_job for reconnect-safe recovery"
        )
        return original_terminate_job

    @staticmethod
    def _should_cancel(request: Request) -> bool:
        if not request.task.acks_late:
            return True
        return not request.acknowledged

    def _cancel_unacknowledged_active_tasks(self) -> None:
        requests_to_cancel: tuple[Request, ...] = tuple(
            request
            for request in state.active_requests
            if self._should_cancel(request)
        )
        if not requests_to_cancel:
            logger.info(
                "Bounded warm shutdown completed without active task cancellation"
            )
            return

        pool: object = self._worker.pool
        if not isinstance(pool, GeventTaskPool):
            logger.error(
                "Cannot cancel active tasks during bounded shutdown: "
                f"expected gevent pool, got {type(pool).__name__}"
            )
            return

        cancellation_pool = _ShutdownCancellationPool(
            pool,
            self._original_terminate_job,
        )
        logger.warning(
            "Bounded warm shutdown timeout expired; cancelling "
            f"{len(requests_to_cancel)} unacknowledged active task(s)"
        )
        for request in requests_to_cancel:
            # Request.cancel performs Celery's normal task-ready bookkeeping,
            # but the adapter invokes gevent's saved original cancellation.
            # The broker connection then closes normally, allowing Kombu to
            # restore each unacknowledged reservation for another worker.
            try:
                request.cancel(cancellation_pool)
            except Exception as exc:
                # The greenlet is terminated before Celery records its retry
                # event. A result-backend outage must not abort this loop and
                # leave later active tasks running until ECS force-kills us.
                logger.warning(
                    "Cancelled task but could not record shutdown retry "
                    f"(task_id={request.id}): {exc}"
                )
