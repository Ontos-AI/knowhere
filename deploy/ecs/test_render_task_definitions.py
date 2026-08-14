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

SHARED_STAGING_ENVIRONMENT: dict[str, str] = {
    "ALI_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    "ARK_URL": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    "BROKER_POOL_LIMIT": "5",
    "DS_URL": "https://api.deepseek.com/v1/chat/completions",
    "EMBEDDING_MODEL": "text-embedding-v4",
    "S3_TEMP_PATH": "/tmp",
    "SUPPORTED_EXTENSIONS": ".doc,.docx,.pdf,.txt,.xls,.xlsx,.csv,.jpg,.jpeg,.png,.pptx,.md",
    "TMP_PATH": "/tmp/aismart_bid",
}

API_STAGING_ENVIRONMENT: dict[str, str] = {
    **SHARED_STAGING_ENVIRONMENT,
    "DB_MAX_OVERFLOW": "5",
    "DB_POOL_SIZE": "5",
}

WORKER_STAGING_ENVIRONMENT: dict[str, str] = {
    **SHARED_STAGING_ENVIRONMENT,
    "ALL_DF_COLS": "content,path,type,length,keywords,summary,know_id,tokens,connectto,addtime,page_nums",
    "DB_SYNC_MAX_OVERFLOW": "2",
    "DB_SYNC_POOL_SIZE": "2",
    "HIERARCHY_LLM_MODEL": "deepseek-chat",
    "IMAGE_MODEL": "qwen3.5-flash",
    "IMAGE_MODEL_MAX": "qwen3.5-flash",
    "MAX_PDF_PAGE_LIMIT": "200",
    "NORMOL_MODEL": "deepseek-chat",
    "OVERSIZED_PDF_SHARD_ENABLED": "true",
    "OVERSIZED_PDF_SOFT_LIMIT": "1500",
    "PDF_PROFILE_TOC_ENABLED": "true",
    "REDIS_SYNC_MAX_CONNECTIONS": "200",
    "SUMMARY_LLM_MAX_CONCURRENT": "8",
    "WORKER_CONCURRENCY": "10",
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


@pytest.mark.parametrize(
    ("template_name", "container_name", "expected_environment"),
    [
        (
            "task-definition-api.staging.json",
            "api",
            API_STAGING_ENVIRONMENT,
        ),
        (
            "task-definition-worker.staging.json",
            "worker",
            WORKER_STAGING_ENVIRONMENT,
        ),
    ],
)
def test_staging_templates_preserve_expected_staging_configuration(
    template_name: str,
    container_name: str,
    expected_environment: dict[str, str],
) -> None:
    """Fargate preserves the verified staging settings captured on 2026-08-13."""
    definition_path: Path = TEMPLATE_DIRECTORY / template_name
    definition: dict[str, object] = json.loads(
        definition_path.read_text(encoding="utf-8")
    )
    container_definitions: list[dict[str, object]] = definition[
        "containerDefinitions"
    ]
    container: dict[str, object] = next(
        item for item in container_definitions if item.get("name") == container_name
    )
    environment: list[dict[str, str]] = container["environment"]
    environment_values: dict[str, str] = {
        item["name"]: item["value"] for item in environment
    }

    for name, value in expected_environment.items():
        assert environment_values[name] == value


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
