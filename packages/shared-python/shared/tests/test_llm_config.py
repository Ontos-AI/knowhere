"""Unit tests for BYOK LLMConfig resolution."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.models.schemas.llm_config import LLMConfig, LLMProviderConfig


def _creds(model: str = "gpt-4o") -> LLMProviderConfig:
    return LLMProviderConfig(
        api_key="sk-test",
        model=model,
        base_url="https://api.openai.com/v1",
    )


def test_provider_alone_applies_to_both_channels() -> None:
    cfg = LLMConfig(provider=_creds("gpt-4o"))
    assert cfg.text_effective() is not None
    assert cfg.vision_effective() is not None
    assert cfg.text_effective().model == "gpt-4o"
    assert cfg.vision_effective().model == "gpt-4o"


def test_text_only_leaves_vision_on_defaults() -> None:
    cfg = LLMConfig(text=_creds("text-model"))
    assert cfg.text_effective().model == "text-model"
    assert cfg.vision_effective() is None


def test_vision_only_leaves_text_on_defaults() -> None:
    cfg = LLMConfig(vision=_creds("vlm"))
    assert cfg.text_effective() is None
    assert cfg.vision_effective().model == "vlm"


def test_channel_overrides_provider() -> None:
    cfg = LLMConfig(
        provider=_creds("shared"),
        text=_creds("text-only"),
        vision=_creds("vision-only"),
    )
    assert cfg.text_effective().model == "text-only"
    assert cfg.vision_effective().model == "vision-only"


def test_provider_plus_text_override() -> None:
    cfg = LLMConfig(provider=_creds("shared"), text=_creds("text-only"))
    assert cfg.text_effective().model == "text-only"
    assert cfg.vision_effective().model == "shared"


def test_empty_config_rejected() -> None:
    with pytest.raises(ValidationError, match="provider, text, or vision"):
        LLMConfig()


def test_masked_dump_includes_provider() -> None:
    cfg = LLMConfig(provider=_creds())
    dump = cfg.masked_dump()
    assert dump["provider"] is not None
    assert dump["provider"]["api_key"] != "sk-test"
    assert dump["text"] is None
    assert dump["vision"] is None
