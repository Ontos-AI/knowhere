from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.models.schemas.page_memory_config import PageMemoryConfig


def test_page_memory_config_defaults_resolve_concurrency_settings() -> None:
    config = PageMemoryConfig.default()

    assert config.scope_concurrency == 5
    assert config.tag_concurrency == 4
    assert config.node_assembly_concurrency == 3
    assert config.asset_extraction_enabled is True
    assert config.asset_summary_enabled is True
