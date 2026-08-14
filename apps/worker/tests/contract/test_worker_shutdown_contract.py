from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from celery import Celery
from pydantic import ValidationError

_PROCESS_CONTRACT_QUEUE_NAME: str = "worker_shutdown_contract"
_PROCESS_CONTRACT_TASK_NAME: str = (
    "worker_shutdown_contract.run_blocking_task"
)
_PROCESS_CONTRACT_SOFT_TIMEOUT_SECONDS: float = 0.25
_PROCESS_CONTRACT_EXIT_DEADLINE_SECONDS: float = 5.0


def _wait_for_started_tasks(
    marker_path: Path,
    worker_process: subprocess.Popen[str],
    task_count: int,
    timeout_seconds: float,
) -> bool:
    deadline: float = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker_path.exists() and len(
            marker_path.read_text(encoding="utf-8").splitlines()
        ) >= task_count:
            return True
        if worker_process.poll() is not None:
            return False
        time.sleep(0.05)
    return marker_path.exists() and len(
        marker_path.read_text(encoding="utf-8").splitlines()
    ) >= task_count


def _stop_process(worker_process: subprocess.Popen[str]) -> str:
    if worker_process.poll() is None:
        worker_process.kill()
        worker_process.wait(timeout=5)

    if worker_process.stdout is None:
        return ""
    return worker_process.stdout.read()


def _start_shutdown_contract_worker(
    repository_root: Path,
    broker_directory: Path,
    started_marker_path: Path,
    heartbeat_path: Path,
    concurrency: int,
    shutdown_timeout_seconds: float,
    fail_result_backend: bool,
) -> subprocess.Popen[str]:
    process_environment: dict[str, str] = os.environ.copy()
    python_paths: tuple[str, ...] = (
        str(repository_root / "apps/worker"),
        str(repository_root / "apps/worker/tests"),
        str(repository_root / "packages/shared-python"),
    )
    existing_python_path: str | None = process_environment.get("PYTHONPATH")
    process_environment["PYTHONPATH"] = os.pathsep.join(
        (*python_paths, existing_python_path)
        if existing_python_path
        else python_paths
    )
    process_environment["WORKER_HEARTBEAT_FILE"] = str(heartbeat_path)
    process_environment["WORKER_SHUTDOWN_CONTRACT_BROKER_DIRECTORY"] = str(
        broker_directory
    )
    process_environment["WORKER_SHUTDOWN_CONTRACT_STARTED_MARKER"] = str(
        started_marker_path
    )
    process_environment["WORKER_SHUTDOWN_CONTRACT_TIMEOUT_SECONDS"] = str(
        shutdown_timeout_seconds
    )
    if fail_result_backend:
        process_environment[
            "WORKER_SHUTDOWN_CONTRACT_FAIL_RESULT_BACKEND"
        ] = "1"
    else:
        process_environment.pop(
            "WORKER_SHUTDOWN_CONTRACT_FAIL_RESULT_BACKEND",
            None,
        )

    worker_command: list[str] = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "support.worker_shutdown_process_app:celery_app",
        "worker",
        "--pool=gevent",
        f"--concurrency={concurrency}",
        "--loglevel=INFO",
        "--hostname=shutdown-contract@%h",
        "-Q",
        _PROCESS_CONTRACT_QUEUE_NAME,
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
    ]
    return subprocess.Popen(
        worker_command,
        env=process_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _create_shutdown_contract_producer(broker_directory: Path) -> Celery:
    producer_app: Celery = Celery(
        "worker-shutdown-contract-producer",
        broker="filesystem://",
    )
    producer_app.conf.broker_transport_options = {
        "data_folder_in": str(broker_directory),
        "data_folder_out": str(broker_directory),
        "control_folder": str(broker_directory / "control"),
        "store_processed": False,
    }
    return producer_app


def _wait_for_worker_exit(
    worker_process: subprocess.Popen[str],
    repeat_signal: bool = False,
) -> tuple[float, str]:
    shutdown_started_at: float = time.monotonic()
    os.kill(worker_process.pid, signal.SIGTERM)
    if repeat_signal:
        time.sleep(0.05)
        os.kill(worker_process.pid, signal.SIGTERM)

    try:
        worker_process.wait(timeout=_PROCESS_CONTRACT_EXIT_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        worker_output: str = _stop_process(worker_process)
        raise AssertionError(
            "warm SIGTERM did not cancel the active gevent task before "
            "the scaled ECS stop deadline\n"
            f"{worker_output}"
        ) from exc

    shutdown_elapsed_seconds: float = time.monotonic() - shutdown_started_at
    return shutdown_elapsed_seconds, _stop_process(worker_process)


def test_should_cancel_an_active_gevent_task_before_the_ecs_stop_deadline(
    tmp_path: Path,
    worker_contract_environment: None,
) -> None:
    broker_directory: Path = tmp_path / "broker"
    broker_directory.mkdir()
    started_marker_path: Path = tmp_path / "task-started"
    worker_process: subprocess.Popen[str] = _start_shutdown_contract_worker(
        repository_root=Path(__file__).resolve().parents[4],
        broker_directory=broker_directory,
        started_marker_path=started_marker_path,
        heartbeat_path=tmp_path / "worker-heartbeat",
        concurrency=1,
        shutdown_timeout_seconds=_PROCESS_CONTRACT_SOFT_TIMEOUT_SECONDS,
        fail_result_backend=False,
    )
    producer_app: Celery = _create_shutdown_contract_producer(broker_directory)

    try:
        producer_app.send_task(
            _PROCESS_CONTRACT_TASK_NAME,
            args=[1, 30.0],
            queue=_PROCESS_CONTRACT_QUEUE_NAME,
        )
        assert _wait_for_started_tasks(
            started_marker_path,
            worker_process,
            task_count=1,
            timeout_seconds=15,
        ), _stop_process(worker_process)

        shutdown_elapsed_seconds, _ = _wait_for_worker_exit(worker_process)
        assert (
            shutdown_elapsed_seconds < _PROCESS_CONTRACT_EXIT_DEADLINE_SECONDS
        )
        assert worker_process.returncode == 0
    finally:
        producer_app.close()
        _stop_process(worker_process)


def test_should_continue_cancelling_tasks_when_result_backend_recording_fails(
    tmp_path: Path,
    worker_contract_environment: None,
) -> None:
    broker_directory: Path = tmp_path / "broker"
    broker_directory.mkdir()
    started_marker_path: Path = tmp_path / "task-started"
    worker_process: subprocess.Popen[str] = _start_shutdown_contract_worker(
        repository_root=Path(__file__).resolve().parents[4],
        broker_directory=broker_directory,
        started_marker_path=started_marker_path,
        heartbeat_path=tmp_path / "worker-heartbeat",
        concurrency=2,
        shutdown_timeout_seconds=_PROCESS_CONTRACT_SOFT_TIMEOUT_SECONDS,
        fail_result_backend=True,
    )
    producer_app: Celery = _create_shutdown_contract_producer(broker_directory)

    try:
        for task_number in (1, 2):
            producer_app.send_task(
                _PROCESS_CONTRACT_TASK_NAME,
                args=[task_number, 30.0],
                queue=_PROCESS_CONTRACT_QUEUE_NAME,
            )
        assert _wait_for_started_tasks(
            started_marker_path,
            worker_process,
            task_count=2,
            timeout_seconds=15,
        ), _stop_process(worker_process)

        shutdown_elapsed_seconds, worker_output = _wait_for_worker_exit(
            worker_process
        )

        assert worker_process.returncode == 0
        assert (
            shutdown_elapsed_seconds < _PROCESS_CONTRACT_EXIT_DEADLINE_SECONDS
        )
        assert worker_output.count("could not record shutdown retry") == 2, worker_output
    finally:
        producer_app.close()
        _stop_process(worker_process)


def test_should_not_cancel_a_task_that_finishes_before_shutdown_timeout(
    tmp_path: Path,
    worker_contract_environment: None,
) -> None:
    broker_directory: Path = tmp_path / "broker"
    broker_directory.mkdir()
    started_marker_path: Path = tmp_path / "task-started"
    worker_process: subprocess.Popen[str] = _start_shutdown_contract_worker(
        repository_root=Path(__file__).resolve().parents[4],
        broker_directory=broker_directory,
        started_marker_path=started_marker_path,
        heartbeat_path=tmp_path / "worker-heartbeat",
        concurrency=1,
        shutdown_timeout_seconds=0.75,
        fail_result_backend=False,
    )
    producer_app: Celery = _create_shutdown_contract_producer(broker_directory)

    try:
        producer_app.send_task(
            _PROCESS_CONTRACT_TASK_NAME,
            args=[1, 0.05],
            queue=_PROCESS_CONTRACT_QUEUE_NAME,
        )
        assert _wait_for_started_tasks(
            started_marker_path,
            worker_process,
            task_count=1,
            timeout_seconds=15,
        ), _stop_process(worker_process)
        time.sleep(0.2)

        _, worker_output = _wait_for_worker_exit(worker_process)

        assert worker_process.returncode == 0
        assert "cancelling 1 unacknowledged active task" not in worker_output
    finally:
        producer_app.close()
        _stop_process(worker_process)


def test_should_schedule_only_one_shutdown_timer_for_repeated_sigterm(
    tmp_path: Path,
    worker_contract_environment: None,
) -> None:
    broker_directory: Path = tmp_path / "broker"
    broker_directory.mkdir()
    started_marker_path: Path = tmp_path / "task-started"
    worker_process: subprocess.Popen[str] = _start_shutdown_contract_worker(
        repository_root=Path(__file__).resolve().parents[4],
        broker_directory=broker_directory,
        started_marker_path=started_marker_path,
        heartbeat_path=tmp_path / "worker-heartbeat",
        concurrency=1,
        shutdown_timeout_seconds=_PROCESS_CONTRACT_SOFT_TIMEOUT_SECONDS,
        fail_result_backend=False,
    )
    producer_app: Celery = _create_shutdown_contract_producer(broker_directory)

    try:
        producer_app.send_task(
            _PROCESS_CONTRACT_TASK_NAME,
            args=[1, 30.0],
            queue=_PROCESS_CONTRACT_QUEUE_NAME,
        )
        assert _wait_for_started_tasks(
            started_marker_path,
            worker_process,
            task_count=1,
            timeout_seconds=15,
        ), _stop_process(worker_process)

        _, worker_output = _wait_for_worker_exit(
            worker_process,
            repeat_signal=True,
        )

        assert worker_process.returncode == 0
        assert worker_output.count(
            "Scheduled bounded warm shutdown cancellation"
        ) == 1
    finally:
        producer_app.close()
        _stop_process(worker_process)


def test_should_keep_gevent_pool_cancellation_disabled_for_broker_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    worker_contract_environment: None,
) -> None:
    import gevent
    from app.core.gevent_worker_shutdown import GeventWorkerShutdownController
    from celery.concurrency.gevent import TaskPool as GeventTaskPool
    from celery.worker import WorkController

    original_terminate_job: Callable[
        [GeventTaskPool, int, int | None], None
    ] = cast(
        Callable[[GeventTaskPool, int, int | None], None],
        getattr(GeventTaskPool, "_original_terminate_job", None),
    )
    if not callable(original_terminate_job):
        original_terminate_job = GeventTaskPool.terminate_job

    pool: GeventTaskPool = GeventTaskPool(1)
    pool.start()
    controller = GeventWorkerShutdownController(
        worker=cast(
            WorkController,
            SimpleNamespace(pool=pool),
        ),
        timeout_seconds=1,
    )

    def wait_for_reconnect_contract() -> None:
        gevent.sleep(10)

    running_greenlet = gevent.spawn(wait_for_reconnect_contract)
    pool._pool_map[id(running_greenlet)] = running_greenlet

    try:
        pool.terminate_job(id(running_greenlet))
        gevent.sleep(0)
        assert running_greenlet.dead is False
    finally:
        controller.close()
        original_terminate_job(pool, id(running_greenlet), None)
        gevent.sleep(0)
        pool.stop()
        monkeypatch.setattr(
            GeventTaskPool,
            "terminate_job",
            original_terminate_job,
        )


def test_should_preserve_fargate_worker_sigterm_shutdown_contract(
    worker_contract_environment: None,
) -> None:
    from shared.core.celery_app import celery_app
    from app.core import worker_bootstrap

    repository_root: Path = Path(__file__).resolve().parents[4]
    task_definition_path: Path = (
        repository_root / "deploy/ecs/task-definition-worker.staging.json"
    )
    task_definition: dict[str, object] = json.loads(
        task_definition_path.read_text(encoding="utf-8")
    )
    container_definitions: list[dict[str, object]] = task_definition[
        "containerDefinitions"
    ]
    worker_container: dict[str, object] = next(
        container
        for container in container_definitions
        if container.get("name") == "worker"
    )
    environment: list[dict[str, str]] = worker_container["environment"]
    environment_values: dict[str, str] = {
        item["name"]: item["value"] for item in environment
    }

    assert celery_app.conf.worker_soft_shutdown_timeout == 90
    assert celery_app.conf.worker_enable_soft_shutdown_on_idle is True
    assert "REMAP_SIGTERM" not in environment_values
    assert worker_container["stopTimeout"] == 120
    maximum_child_cleanup_seconds: float = 2 * (
        worker_bootstrap._CHILD_PROCESS_TERM_TIMEOUT_SECONDS
        + worker_bootstrap._CHILD_PROCESS_KILL_TIMEOUT_SECONDS
    )
    assert (
        celery_app.conf.worker_soft_shutdown_timeout
        + maximum_child_cleanup_seconds
        < worker_container["stopTimeout"]
    )


def test_should_redeliver_interrupted_tasks_before_processing_jobs_expire(
    worker_contract_environment: None,
) -> None:
    from shared.core.celery_app import celery_app
    from shared.core.config.job import JobConfig

    task_time_limit_seconds: int = celery_app.conf.task_time_limit
    visibility_timeout_seconds: int = celery_app.conf.broker_transport_options[
        "visibility_timeout"
    ]
    processing_expiry_seconds: int = JobConfig().JOB_PROCESSING_EXPIRE_SECONDS

    assert task_time_limit_seconds == 3600
    assert visibility_timeout_seconds == 4500
    assert processing_expiry_seconds == 14400
    assert (
        task_time_limit_seconds
        < visibility_timeout_seconds
        < processing_expiry_seconds
    )


def test_should_reject_processing_expiry_before_interrupted_task_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    worker_contract_environment: None,
) -> None:
    from shared.core.config import AppConfig

    monkeypatch.setenv("JOB_PROCESSING_EXPIRE_SECONDS", "4500")

    with pytest.raises(
        ValidationError,
        match="TASK_TIME_LIMIT_SECONDS < BROKER_VISIBILITY_TIMEOUT_SECONDS",
    ):
        AppConfig()
