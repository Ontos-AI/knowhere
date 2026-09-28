from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.config import settings
from shared.models.schemas.page_memory_config import (
    PageMemoryConfig,
    resolve_asset_model,
)


def test_page_memory_config_defaults_resolve_concurrency_settings() -> None:
    config = PageMemoryConfig.default()

    assert config.scope_concurrency == 5
    assert config.tag_concurrency == 4
    assert config.title_detection_concurrency == 3
    assert config.node_assembly_concurrency == 3
    assert config.asset_model == ""


def test_resolve_asset_model_follows_image_model_when_unset(monkeypatch) -> None:
    monkeypatch.setattr(settings, "IMAGE_MODEL", "vision-a")
    monkeypatch.setattr(settings, "ASSET_MODEL", "")

    assert resolve_asset_model() == "vision-a"


def test_resolve_asset_model_uses_asset_model_override(monkeypatch) -> None:
    monkeypatch.setattr(settings, "IMAGE_MODEL", "vision-a")
    monkeypatch.setattr(settings, "ASSET_MODEL", "vision-b")

    assert resolve_asset_model() == "vision-b"


def test_page_memory_config_from_mapping_keeps_explicit_asset_model() -> None:
    config = PageMemoryConfig.from_mapping({"asset_model": "vision-b"})

    assert config.asset_model == "vision-b"
