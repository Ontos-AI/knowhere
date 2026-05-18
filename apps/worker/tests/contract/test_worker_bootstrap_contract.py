from __future__ import annotations

import sys

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
    beat_commands: list[list[str]] = []

    class FakeBeatProcess:
        pass

    def record_beat_command(command: list[str]) -> FakeBeatProcess:
        beat_commands.append(command)
        return FakeBeatProcess()

    def record_worker_main(args: list[str]) -> None:
        worker_main_calls.append(args)

    monkeypatch.setattr(worker_bootstrap.subprocess, "Popen", record_beat_command)
    monkeypatch.setattr(worker_bootstrap.celery_app, "worker_main", record_worker_main)

    worker_bootstrap.run_worker()

    assert beat_commands != []
    assert len(worker_main_calls) == 1

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
