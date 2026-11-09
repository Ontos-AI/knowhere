"""Unit tests for DatabaseConfig pool defaults."""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.config.database import DatabaseConfig


def test_database_pool_defaults_are_fifty() -> None:
    config: DatabaseConfig = DatabaseConfig(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
    )
    assert config.DB_POOL_SIZE == 50
    assert config.DB_MAX_OVERFLOW == 50
    assert config.DB_POOL_PREWARM_SIZE == 5


@pytest.mark.parametrize(("pool_size", "prewarm_size", "expected"), [(15, 15, 15), (4, 15, 4), (15, 0, 0)])
def test_prewarm_respects_configured_count_and_pool_capacity(
    monkeypatch: pytest.MonkeyPatch, pool_size: int, prewarm_size: int, expected: int
) -> None:
    from shared.core import database

    config: DatabaseConfig = DatabaseConfig(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        DB_POOL_SIZE=pool_size,
        DB_POOL_PREWARM_SIZE=prewarm_size,
    )
    attempts: list[None] = []

    async def warm_connection() -> None:
        attempts.append(None)

    monkeypatch.setattr(database, "settings", config)
    monkeypatch.setattr(database, "_warm_connection", warm_connection)
    asyncio.run(database.prewarm_connection_pool())

    assert len(attempts) == expected
