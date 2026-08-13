"""Contracts for ECS task-definition rendering."""

import json
from pathlib import Path

import pytest

from render_task_definitions import (
    render_template,
    validate_rendered_definition,
)


TEMPLATE_DIRECTORY: Path = Path(__file__).parent
RENDER_VARIABLES: dict[str, str] = {
    "API_IMAGE": "api-image",
    "WORKER_IMAGE": "worker-image",
    "EXECUTION_ROLE_ARN": "execution-role",
    "API_TASK_ROLE_ARN": "api-role",
    "WORKER_TASK_ROLE_ARN": "worker-role",
    "STAGING_SECRETS_ARN": "secrets-arn",
}


@pytest.mark.parametrize(
    "template_name",
    ["task-definition-api.staging.json", "task-definition-worker.staging.json"],
)
def test_staging_templates_render_without_long_lived_s3_keys(
    tmp_path: Path,
    template_name: str,
) -> None:
    """Both templates render and exclude explicit S3 credential variables."""
    output_path: Path = tmp_path / template_name
    render_template(
        TEMPLATE_DIRECTORY / template_name,
        output_path,
        RENDER_VARIABLES,
    )

    rendered: dict[str, object] = json.loads(output_path.read_text(encoding="utf-8"))
    validate_rendered_definition(rendered)
    assert rendered["requiresCompatibilities"] == ["FARGATE"]
    assert "${" not in output_path.read_text(encoding="utf-8")


def test_renderer_rejects_forbidden_s3_credential_variable() -> None:
    """Task definitions must never inject long-lived S3 credentials."""
    definition: dict[str, object] = {
        "containerDefinitions": [
            {"environment": [{"name": "S3_ACCESS_KEY_ID", "value": "bad"}]}
        ]
    }

    with pytest.raises(ValueError, match="forbidden S3 credentials"):
        validate_rendered_definition(definition)


def test_renderer_rejects_unresolved_placeholders() -> None:
    """Deployment cannot proceed with missing substitution values."""
    definition: dict[str, object] = {
        "containerDefinitions": [{"environment": []}],
        "image": "${API_IMAGE}",
    }

    with pytest.raises(ValueError, match="Unresolved task-definition placeholders"):
        validate_rendered_definition(definition)
