"""Unit tests for DatabaseConfig pool defaults."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.config.database import DatabaseConfig


def test_database_pool_defaults_are_fifty() -> None:
    config = DatabaseConfig(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
    )
    assert config.DB_POOL_SIZE == 50
    assert config.DB_MAX_OVERFLOW == 50
