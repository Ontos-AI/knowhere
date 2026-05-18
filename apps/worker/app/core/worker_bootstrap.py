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


def _register_task_modules() -> None:
    """Import task modules for Celery side-effect registration."""
    import app.core.tasks.kb_tasks  # noqa: F401
    import app.core.tasks.stale_job_sweeper  # noqa: F401
    import app.core.tasks.webhook_tasks  # noqa: F401


@worker_init.connect
def init_worker(**kwargs) -> None:
    """Initialize structured logging and sync Redis when worker process starts."""
    setup_logging(service_name="knowhere-worker")
    start_worker_heartbeat()

    # Celery gevent cannot cancel greenlets on transport reconnect, so use a no-op.
    try:
        from celery.concurrency.gevent import TaskPool as GeventTaskPool

        if not hasattr(GeventTaskPool, "_original_terminate_job"):

            def _graceful_terminate_job(self, pid, signal=None):
                logger.warning(
                    f"gevent pool cannot kill greenlet (pid={pid}), "
                    f"relying on RedisJobLock for dedup on redelivery"
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
            GeventTaskPool.terminate_job = _graceful_terminate_job
            logger.info("Patched gevent TaskPool.terminate_job for graceful recovery")
    except Exception as exc:
        logger.warning(f"Could not patch gevent TaskPool: {exc}")

    try:
        from shared.services.redis.redis_sync_service import SyncRedisServiceFactory

        service = SyncRedisServiceFactory.get_service()
        if service.ping():
            logger.info("Worker sync Redis connection verified")
        else:
            logger.warning("Worker sync Redis ping failed, will retry on first use")
    except Exception as exc:
        logger.warning(f"Worker sync Redis init deferred: {exc}")


@worker_shutdown.connect
def shutdown_worker(**kwargs) -> None:
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
    """Start the gevent Celery worker and its colocated Beat process.

    Every worker replica unconditionally spawns a Celery Beat subprocess.
    RedBeat's own distributed lock (``redbeat_lock_timeout`` /
    ``beat_max_loop_interval``) ensures that only one Beat instance actually
    drives the scheduler tick loop — all other instances block on lock
    acquisition and remain idle.

    Even if the RedBeat startup-burst window allows multiple Beat instances
    to enqueue the same periodic task simultaneously, each task body is
    guarded by a ``periodic_task_lock`` (Redis ``SET NX EX``) keyed on the
    task name.  Only the first invocation within each scheduling window
    executes; all subsequent duplicates log a skip and return immediately.
    """
    from shared.core.config import settings

    _register_task_modules()

    hostname = socket.gethostname()
    pid = os.getpid()
    node_name = f"celery@{hostname}-{pid}"
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    concurrency = settings.WORKER_CONCURRENCY

    celery_args = [
        "worker",
        "--pool=gevent",
        f"--concurrency={concurrency}",
        f"--loglevel={log_level}",
        f"--hostname={node_name}",
        "-Q",
        "kb_high,kb_medium,kb_low,ai_high_priority,default",
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

    logger.info("Starting Celery Beat subprocess")
    subprocess.Popen(beat_cmd)

    celery_app.worker_main(celery_args)
