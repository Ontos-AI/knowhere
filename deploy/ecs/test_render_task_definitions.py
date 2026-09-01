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
    "SECRETS_ARN": "secrets-arn",
    "DEPLOYMENT_ENVIRONMENT": "staging",
    "RUNTIME_ENVIRONMENT": "staging",
    "APP_ENV": "staging",
    "DB_SSL_MODE": "require",
    "API_DB_POOL_SIZE": "5",
    "API_DB_MAX_OVERFLOW": "5",
    "WORKER_DB_SYNC_POOL_SIZE": "2",
    "WORKER_DB_SYNC_MAX_OVERFLOW": "2",
    "S3_BUCKET_NAME": "knowhere-storage-staging",
    "INTERNAL_DASHBOARD_ENDPOINT": "https://staging.knowhereto.ai",
    "FRONTEND_URL": "https://staging.knowhereto.ai",
    "API_WEBHOOK_ENDPOINT": "https://api-staging.knowhereto.ai/v1/internal/s3-events",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:107424103509:knowhere-staging-s3-events",
    "QSTASH_CALLBACK_BASE_URL": "https://api-staging.knowhereto.ai/api/v1",
    "API_CPU": "256",
    "API_MEMORY": "1024",
    "WORKER_CPU": "2048",
    "WORKER_MEMORY": "4096",
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
    tmp_path: Path,
    template_name: str,
    container_name: str,
    expected_environment: dict[str, str],
) -> None:
    """Fargate preserves the verified staging settings captured on 2026-08-13."""
    output_path: Path = tmp_path / template_name
    render_template(
        TEMPLATE_DIRECTORY / template_name,
        output_path,
        RENDER_VARIABLES,
    )
    definition: dict[str, object] = json.loads(output_path.read_text(encoding="utf-8"))
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


def test_staging_worker_preserves_evidence_selected_capacity(tmp_path: Path) -> None:
    """Worker capacity matches the staging load evidence recorded in issue 22."""
    output_path: Path = tmp_path / "task-definition-worker.staging.json"
    render_template(
        TEMPLATE_DIRECTORY / "task-definition-worker.staging.json",
        output_path,
        RENDER_VARIABLES,
    )
    definition: dict[str, object] = json.loads(output_path.read_text(encoding="utf-8"))

    # The measured production-envelope replay rejected 1-vCPU tasks and passed
    # on two fixed 2-vCPU tasks, so CD must not restore the rejected task size.
    assert definition["cpu"] == "2048"
    assert definition["memory"] == "4096"


def test_staging_api_preserves_selected_capacity(tmp_path: Path) -> None:
    """API capacity remains at the verified staging size."""
    output_path: Path = tmp_path / "task-definition-api.staging.json"
    render_template(
        TEMPLATE_DIRECTORY / "task-definition-api.staging.json",
        output_path,
        RENDER_VARIABLES,
    )
    definition: dict[str, object] = json.loads(output_path.read_text(encoding="utf-8"))

    assert definition["cpu"] == "256"
    assert definition["memory"] == "1024"


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
