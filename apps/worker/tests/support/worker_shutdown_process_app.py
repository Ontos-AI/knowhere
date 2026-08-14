"""Minimal Celery application for the worker shutdown process contract."""

from __future__ import annotations

# This process must reproduce the production worker's cooperative runtime.
# Patching after Celery or Redis imports would leave blocking sockets in place.
import gevent.monkey

gevent.monkey.patch_all()

import os
from pathlib import Path

import gevent
from celery.worker.request import Request
from kombu import Queue

# Importing the production bootstrap registers its Celery lifecycle receivers.
# The contract deliberately exercises those receivers instead of a test-only hook.
import app.core.worker_bootstrap  # noqa: F401
from shared.core.celery_app import celery_app

_BROKER_DIRECTORY_ENVIRONMENT_VARIABLE: str = (
    "WORKER_SHUTDOWN_CONTRACT_BROKER_DIRECTORY"
)
_STARTED_MARKER_ENVIRONMENT_VARIABLE: str = (
    "WORKER_SHUTDOWN_CONTRACT_STARTED_MARKER"
)
_SHUTDOWN_TIMEOUT_ENVIRONMENT_VARIABLE: str = (
    "WORKER_SHUTDOWN_CONTRACT_TIMEOUT_SECONDS"
)
_FAIL_RESULT_BACKEND_ENVIRONMENT_VARIABLE: str = (
    "WORKER_SHUTDOWN_CONTRACT_FAIL_RESULT_BACKEND"
)
_QUEUE_NAME: str = "worker_shutdown_contract"
_TASK_NAME: str = "worker_shutdown_contract.run_blocking_task"

broker_directory: Path = Path(
    os.environ[_BROKER_DIRECTORY_ENVIRONMENT_VARIABLE]
)
started_marker_path: Path = Path(
    os.environ[_STARTED_MARKER_ENVIRONMENT_VARIABLE]
)
shutdown_timeout_seconds: float = float(
    os.environ[_SHUTDOWN_TIMEOUT_ENVIRONMENT_VARIABLE]
)

celery_app.conf.broker_url = "filesystem://"
celery_app.conf.result_backend = "cache+memory://"
celery_app.conf.broker_transport_options = {
    "data_folder_in": str(broker_directory),
    "data_folder_out": str(broker_directory),
    "control_folder": str(broker_directory / "control"),
    "store_processed": False,
}
celery_app.conf.task_default_queue = _QUEUE_NAME
celery_app.conf.task_queues = (Queue(_QUEUE_NAME),)
celery_app.conf.task_routes = {_TASK_NAME: {"queue": _QUEUE_NAME}}
celery_app.conf.worker_soft_shutdown_timeout = shutdown_timeout_seconds
celery_app.conf.worker_enable_soft_shutdown_on_idle = True


def _raise_result_backend_failure(*args: object, **kwargs: object) -> None:
    raise RuntimeError("shutdown contract result backend unavailable")


if os.getenv(_FAIL_RESULT_BACKEND_ENVIRONMENT_VARIABLE) == "1":
    # Request.cancel kills the greenlet before it records retry state. Force a
    # bookkeeping failure after that call so the contract proves later tasks
    # are still canceled when the result backend is unavailable.
    celery_app.backend.mark_as_retry = _raise_result_backend_failure
    _original_request_cancel = Request.cancel

    def _cancel_then_fail(
        request: Request,
        pool: object,
        signal: int | None = None,
    ) -> None:
        _original_request_cancel(request, pool, signal)
        raise RuntimeError("shutdown contract retry bookkeeping failed")

    Request.cancel = _cancel_then_fail


@celery_app.task(name=_TASK_NAME, acks_late=True)
def run_blocking_task(task_number: int, duration_seconds: float) -> None:
    """Remain active until the shutdown contract cancels this greenlet."""
    with started_marker_path.open("a", encoding="utf-8") as marker_file:
        marker_file.write(f"{task_number}\n")
        marker_file.flush()
    gevent.sleep(duration_seconds)
