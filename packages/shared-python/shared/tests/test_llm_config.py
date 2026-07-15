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


def _creds(
    model: str = "gpt-4o",
    *,
    api_key: str = "sk-test",
    base_url: str = "https://api.openai.com/v1",
) -> LLMProviderConfig:
    return LLMProviderConfig(api_key=api_key, model=model, base_url=base_url)


def test_flat_root_applies_to_both_channels() -> None:
    cfg = LLMConfig(
        api_key="sk-root",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
    )
    assert cfg.text_effective() is not None
    assert cfg.vision_effective() is not None
    assert cfg.text_effective().model == "gpt-4o"
    assert cfg.vision_effective().api_key == "sk-root"


def test_text_only_leaves_vision_on_defaults() -> None:
    cfg = LLMConfig(text=_creds("text-model"))
    assert cfg.text_effective().model == "text-model"
    assert cfg.vision_effective() is None


def test_vision_only_leaves_text_on_defaults() -> None:
    cfg = LLMConfig(vision=_creds("vlm"))
    assert cfg.text_effective() is None
    assert cfg.vision_effective().model == "vlm"


def test_two_different_endpoints() -> None:
    cfg = LLMConfig(
        text=_creds("gpt-4o-mini", base_url="https://api.openai.com/v1"),
        vision=_creds(
            "qwen-vl-max",
            api_key="sk-ali",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    )
    assert cfg.text_effective().base_url == "https://api.openai.com/v1"
    assert cfg.vision_effective().base_url.endswith("/compatible-mode/v1")
    assert cfg.vision_effective().model == "qwen-vl-max"


def test_channel_replaces_root() -> None:
    cfg = LLMConfig(
        api_key="sk-root",
        model="shared",
        base_url="https://api.openai.com/v1",
        text=_creds("text-only"),
        vision=_creds("vision-only", base_url="https://other.example/v1"),
    )
    assert cfg.text_effective().model == "text-only"
    assert cfg.vision_effective().model == "vision-only"
    assert cfg.vision_effective().base_url == "https://other.example/v1"


def test_root_plus_text_override() -> None:
    cfg = LLMConfig(
        api_key="sk-root",
        model="shared",
        base_url="https://api.openai.com/v1",
        text=_creds("text-only"),
    )
    assert cfg.text_effective().model == "text-only"
    assert cfg.vision_effective().model == "shared"


def test_partial_root_rejected() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        LLMConfig(api_key="sk-only")


def test_empty_config_rejected() -> None:
    with pytest.raises(ValidationError, match="root credentials and/or text/vision"):
        LLMConfig()


def test_masked_dump_masks_root_and_channels() -> None:
    cfg = LLMConfig(
        api_key="sk-root",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        vision=_creds("vlm", api_key="sk-vision"),
    )
    dump = cfg.masked_dump()
    assert dump["api_key"] != "sk-root"
    assert dump["model"] == "gpt-4o"
    assert dump["vision"]["api_key"] != "sk-vision"
    assert dump["text"] is None
