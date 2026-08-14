from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_should_preserve_fargate_worker_sigterm_shutdown_contract(
    worker_contract_environment: None,
) -> None:
    from shared.core.celery_app import celery_app

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
