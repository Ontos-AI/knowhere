from __future__ import annotations

import json
from pathlib import Path


def test_should_preserve_fargate_worker_soft_shutdown_contract(
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
    assert environment_values["REMAP_SIGTERM"] == "SIGQUIT"
    assert worker_container["stopTimeout"] == 120
