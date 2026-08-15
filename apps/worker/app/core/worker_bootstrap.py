"""Bootstrap the full Celery worker runtime only in the main worker process."""

import os
import socket
import subprocess
import sys

from celery.signals import worker_init, worker_shutdown
from loguru import logger

from shared.core.celery_app import celery_app
from shared.core.logging import setup_logging
from shared.services.worker_health import start_worker_heartbeat, stop_worker_heartbeat

_CHILD_PROCESS_TERM_TIMEOUT_SECONDS: float = 5
_CHILD_PROCESS_KILL_TIMEOUT_SECONDS: float = 5


def _register_task_modules() -> None:
    """Import task modules for Celery side-effect registration."""
    import app.core.tasks.document_ingestion_tasks  # noqa: F401
    import app.core.tasks.stale_job_sweeper  # noqa: F401
    import app.core.tasks.webhook_tasks  # noqa: F401


def _stop_child_process(
    process: subprocess.Popen[bytes],
    process_name: str,
) -> None:
    """Stop a colocated worker child without exceeding the ECS stop window."""
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=_CHILD_PROCESS_TERM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning(f"{process_name} did not stop after SIGTERM; killing it")
        process.kill()
        process.wait(timeout=_CHILD_PROCESS_KILL_TIMEOUT_SECONDS)


@worker_init.connect
def init_worker(**kwargs: object) -> None:
    """Initialize structured logging and sync Redis when worker process starts."""
    setup_logging(service_name="knowhere-worker")
    start_worker_heartbeat()

    # Do not cancel gevent tasks from Celery's reconnect or shutdown lifecycle.
    # Staging proved that forced greenlet cancellation races with parser child
    # processes and still misses the ECS stop deadline. Interrupted reservations
    # instead recover through the independent Redis visibility watchdog.
    try:
        from celery.concurrency.gevent import TaskPool as GeventTaskPool

        if not hasattr(GeventTaskPool, "_original_terminate_job"):

            def _ignore_gevent_cancellation(
                self: GeventTaskPool,
                pid: int,
                signal: int | None = None,
            ) -> None:
                logger.warning(
                    f"gevent pool cannot kill greenlet (pid={pid}), "
                    "relying on visibility recovery and RedisJobLock for "
                    "redelivery deduplication"
                )

            setattr(
                GeventTaskPool,
                "_original_terminate_job",
                getattr(
                    GeventTaskPool,
                    "terminate_job",
                    None,
                ),
            )
            GeventTaskPool.terminate_job = _ignore_gevent_cancellation
            logger.info(
                "Patched gevent TaskPool.terminate_job for visibility recovery"
            )
    except Exception as exc:
        logger.warning(f"Could not patch gevent TaskPool: {exc}")

    try:
        from shared.services.redis.redis_sync_service import (
            SyncRedisService,
            SyncRedisServiceFactory,
        )

        service: SyncRedisService = SyncRedisServiceFactory.get_service()
        if service.ping():
            logger.info("Worker sync Redis connection verified")
        else:
            logger.warning("Worker sync Redis ping failed, will retry on first use")
    except Exception as exc:
        logger.warning(f"Worker sync Redis init deferred: {exc}")


@worker_shutdown.connect
def shutdown_worker(**kwargs: object) -> None:
    """Clean up shared resources on worker shutdown."""
    try:
        stop_worker_heartbeat()
        logger.info("Worker heartbeat stopped")
    except Exception as exc:
        logger.warning(f"Worker heartbeat cleanup failed: {exc}")

    try:
        from shared.services.http.client_pool import close_sync_client

        close_sync_client()
        logger.info("Worker sync HTTP client closed")
    except Exception as exc:
        logger.warning(f"Worker HTTP client cleanup failed: {exc}")


def run_worker() -> None:
    """Start Celery with colocated Beat and visibility-recovery processes.

    Every worker replica unconditionally spawns a Celery Beat subprocess.
    RedBeat's own distributed lock (``redbeat_lock_timeout`` /
    ``beat_max_loop_interval``) ensures that only one Beat instance actually
    drives the scheduler tick loop — all other instances block on lock
    acquisition and remain idle.

    Each replica also starts an independent visibility-recovery watchdog.
    Recovery runs outside the Celery gevent pool so ingestion saturation cannot
    starve it. The watchdogs coordinate through the application Redis periodic
    lock, while Kombu's broker mutex protects the restoration transaction.
    """
    from shared.core.config import settings

    _register_task_modules()

    hostname = socket.gethostname()
    pid = os.getpid()
    node_name = f"celery@{hostname}-{pid}"
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    concurrency = settings.WORKER_CONCURRENCY
    worker_queues = ",".join(
        [
            "document_ingestion_high",
            "document_ingestion_medium",
            "document_ingestion_low",
            "kb_high",
            "kb_medium",
            "kb_low",
            "ai_high_priority",
            "default",
        ]
    )

    celery_args = [
        "worker",
        "--pool=gevent",
        f"--concurrency={concurrency}",
        f"--loglevel={log_level}",
        f"--hostname={node_name}",
        "-Q",
        worker_queues,
        "--without-gossip",
        "--without-mingle",
    ]

    beat_cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "shared.core.celery_app",
        "beat",
        f"--loglevel={log_level}",
    ]
    visibility_recovery_cmd: list[str] = [
        sys.executable,
        "-m",
        "app.core.visibility_recovery_watchdog",
    ]

    child_processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    try:
        logger.info("Starting Celery Beat subprocess")
        beat_process: subprocess.Popen[bytes] = subprocess.Popen(beat_cmd)
        child_processes.append(("Celery Beat", beat_process))

        logger.info("Starting visibility recovery watchdog subprocess")
        recovery_process: subprocess.Popen[bytes] = subprocess.Popen(
            visibility_recovery_cmd
        )
        child_processes.append(("Visibility recovery watchdog", recovery_process))

        celery_app.worker_main(celery_args)
    finally:
        for process_name, child_process in reversed(child_processes):
            _stop_child_process(child_process, process_name)
