"""Index readiness publish must not hold up map-unit discovery."""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.search import map_unit_discovery


@pytest.mark.asyncio
async def test_schedule_index_readiness_returns_before_redis_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    released = asyncio.Event()

    async def _slow_record(**_kwargs: object) -> None:
        started.set()
        await released.wait()

    monkeypatch.setattr(map_unit_discovery, "record_retrieval_index_readiness", _slow_record)
    map_unit_discovery._schedule_index_readiness(
        user_id="user",
        namespace="default",
        ready=True,
        expected_revisions=1,
        indexed_revisions=1,
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert not released.is_set()
    released.set()
    await asyncio.sleep(0)
