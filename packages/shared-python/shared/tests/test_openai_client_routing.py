"""Unit tests for OpenAI-compatible provider credential routing (no live API)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.ai.openai_compatible_client_sync import (  # noqa: E402
    OpenAICompatibleClientSync,
)


def _client_with(model: str) -> OpenAICompatibleClientSync:
    return OpenAICompatibleClientSync(default_model=model)


def test_orcarouter_model_resolves_base_url_and_api_key() -> None:
    """orcarouter/* models route to the OrcaRouter endpoint and ORCA_API_KEY."""
    fake_settings = SimpleNamespace(
        ORCA_URL="https://api.orcarouter.ai/v1",
        ORCA_API_KEY="orca-test-key",
        DS_URL="https://api.deepseek.com/v1",
        DS_KEY="ds-test-key",
    )
    client = _client_with("orcarouter/fusion")
    with patch(
        "shared.services.ai.openai_compatible_client_sync.settings",
        fake_settings,
    ):
        assert client._resolve_base_url("orcarouter/fusion", None) == (
            "https://api.orcarouter.ai/v1"
        )
        assert client._resolve_direct_api_key("orcarouter/fusion", None) == (
            "orca-test-key"
        )


def test_non_orcarouter_model_keeps_existing_routing() -> None:
    """Models without the orcarouter prefix keep the DeepSeek defaults."""
    fake_settings = SimpleNamespace(
        DS_URL="https://api.deepseek.com/v1",
        DS_KEY="ds-test-key",
        GLM_URL="https://open.bigmodel.cn/api/paas/v4",
        GLM_API_KEY="glm-test-key",
    )
    client = _client_with("deepseek-v4-flash")
    with patch(
        "shared.services.ai.openai_compatible_client_sync.settings",
        fake_settings,
    ):
        assert client._resolve_base_url("deepseek-v4-flash", None) == (
            "https://api.deepseek.com/v1"
        )
        assert client._resolve_direct_api_key("deepseek-v4-flash", None) == (
            "ds-test-key"
        )
