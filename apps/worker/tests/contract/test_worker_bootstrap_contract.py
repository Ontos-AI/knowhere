from __future__ import annotations

import subprocess
import sys

import pytest
from pytest import MonkeyPatch


def test_should_register_worker_task_modules_for_celery_consumers(
    worker_contract_environment: None,
) -> None:
    from app.core import worker_bootstrap
    from shared.core.celery_app import celery_app

    expected_task_names: tuple[str, ...] = (
        "app.core.tasks.document_ingestion_tasks.upload_url_file_task",
        "app.core.tasks.document_ingestion_tasks.parse_task",
        "app.core.tasks.kb_tasks.upload_url_file_task",
        "app.core.tasks.kb_tasks.parse_task",
        "app.core.tasks.stale_job_sweeper.expire_stale_jobs",
        "app.core.tasks.webhook_tasks.recover_orphaned_webhooks",
    )
    task_module_names: tuple[str, ...] = (
        "app.core.tasks.document_ingestion_tasks",
        "app.core.tasks.stale_job_sweeper",
        "app.core.tasks.webhook_tasks",
    )

    for task_name in expected_task_names:
        celery_app.tasks.pop(task_name, None)

    for module_name in task_module_names:
        sys.modules.pop(module_name, None)

    worker_bootstrap._register_task_modules()

    for task_name in expected_task_names:
        assert task_name in celery_app.tasks


def test_should_consume_current_and_legacy_ingestion_queues(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import worker_bootstrap

    worker_main_calls: list[list[str]] = []
    child_process_commands: list[list[str]] = []
    child_processes: list[FakeChildProcess] = []

    class FakeChildProcess:
        def __init__(self) -> None:
            self.was_terminated: bool = False
            self.was_killed: bool = False
            self.wait_timeouts: list[float] = []

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.was_terminated = True

        def kill(self) -> None:
            self.was_killed = True

        def wait(self, *, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            return 0

    def record_child_process(command: list[str]) -> FakeChildProcess:
        child_process_commands.append(command)
        process = FakeChildProcess()
        child_processes.append(process)
        return process

    def record_worker_main(args: list[str]) -> None:
        worker_main_calls.append(args)

    monkeypatch.setattr(worker_bootstrap.subprocess, "Popen", record_child_process)
    monkeypatch.setattr(worker_bootstrap.celery_app, "worker_main", record_worker_main)

    worker_bootstrap.run_worker()

    assert len(child_process_commands) == 2
    assert len(worker_main_calls) == 1

    assert any("beat" in command for command in child_process_commands)
    assert [
        sys.executable,
        "-m",
        "app.core.visibility_recovery_watchdog",
    ] in child_process_commands
    assert all(process.was_terminated for process in child_processes)
    assert all(process.wait_timeouts == [5] for process in child_processes)
    assert all(not process.was_killed for process in child_processes)

    worker_args = worker_main_calls[0]
    queue_arg = worker_args[worker_args.index("-Q") + 1]
    consumed_queues = set(queue_arg.split(","))

    assert {
        "document_ingestion_high",
        "document_ingestion_medium",
        "document_ingestion_low",
        "kb_high",
        "kb_medium",
        "kb_low",
    }.issubset(consumed_queues)

    assert "worker_maintenance" not in consumed_queues


def test_should_kill_a_child_that_ignores_sigterm(
    worker_contract_environment: None,
) -> None:
    from app.core import worker_bootstrap

    class StubbornChildProcess:
        def __init__(self) -> None:
            self.was_terminated: bool = False
            self.was_killed: bool = False
            self.wait_timeouts: list[float] = []

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.was_terminated = True

        def kill(self) -> None:
            self.was_killed = True

        def wait(self, *, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                raise subprocess.TimeoutExpired("stubborn-child", timeout)
            return 0

    process: StubbornChildProcess = StubbornChildProcess()

    worker_bootstrap._stop_child_process(process, "Stubborn child")

    assert process.was_terminated is True
    assert process.was_killed is True
    assert process.wait_timeouts == [5, 5]


def test_should_stop_beat_when_the_watchdog_fails_to_start(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core import worker_bootstrap

    class FakeBeatProcess:
        def __init__(self) -> None:
            self.was_terminated: bool = False
            self.wait_timeouts: list[float] = []

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.was_terminated = True

        def kill(self) -> None:
            raise AssertionError("responsive Beat must not be killed")

        def wait(self, *, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            return 0

    beat_process: FakeBeatProcess = FakeBeatProcess()
    spawn_count: int = 0

    def fail_watchdog_spawn(command: list[str]) -> FakeBeatProcess:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            return beat_process
        raise OSError("watchdog spawn failed")

    monkeypatch.setattr(worker_bootstrap.subprocess, "Popen", fail_watchdog_spawn)

    with pytest.raises(OSError, match="watchdog spawn failed"):
        worker_bootstrap.run_worker()

    assert beat_process.was_terminated is True
    assert beat_process.wait_timeouts == [5]
