"""Exercise API cleanup on exceptional shutdown without opening services."""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-kw04-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("doesApplicationFail", [False, True])
async def test_api_lifespan_closes_native_runtime_after_exception(monkeypatch: pytest.MonkeyPatch, doesApplicationFail: bool) -> None:
    from shared.core.config import redis_pool_manager, settings
    from shared.core import database, logging
    from shared.services.http import client_pool
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
    from shared.services.retrieval.lexical.recency import LexicalRecency
    from shared.services.retrieval.lexical.preparation import LexicalPreparation
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
    from shared.services.storage.async_presigner import AsyncStoragePresigner
    from shared.services.retrieval.stats import recorder
    from shared.services.telemetry import aggregates, runtime

    events: list[str] = []
    monkeypatch.setattr(settings, "LOGFIRE_TOKEN", "")
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setattr(logging, "setup_logging", lambda **arguments: None)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "apps" / "api"))
    apiModule: ModuleType = importlib.import_module("main")
    from app.services.rate_limit.config import RateLimitConfig

    @asynccontextmanager
    async def databaseContext() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(run_sync=AsyncMock())

    async def flush(bind: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("flusher_stopped")

    async def closeNative() -> None:
        events.append("native_closed")

    async def closeHttp() -> None:
        events.append("http_closed")

    async def dispose() -> None:
        events.append("database_disposed")

    monkeypatch.setattr(database, "prewarm_connection_pool", AsyncMock())
    monkeypatch.setattr(database, "get_db_context", databaseContext)
    monkeypatch.setattr(redis_pool_manager, "init_pool", AsyncMock())
    monkeypatch.setattr(type(redis_pool_manager.config), "get_connection_url", lambda self: "redis://offline")
    monkeypatch.setattr(RateLimitConfig, "get_instance", lambda *arguments: None)
    monkeypatch.setattr(apiModule, "load_rules", AsyncMock())
    monkeypatch.setattr(runtime, "start_self_hosted_telemetry", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "stop_self_hosted_heartbeat_telemetry", AsyncMock())
    monkeypatch.setattr(runtime, "stop_self_hosted_telemetry", AsyncMock())
    monkeypatch.setattr(aggregates, "stop_self_hosted_aggregate_telemetry", AsyncMock())
    monkeypatch.setattr(recorder, "drain_retrieval_hit_stats_updates", AsyncMock())
    monkeypatch.setattr(LexicalRecency, "runFlushes", flush)
    monkeypatch.setattr(LexicalPreparation, "warmCompiler", lambda: events.append("compiler_warmed"))
    monkeypatch.setattr(AsyncOpenSearchRuntime, "closeCurrent", closeNative)
    monkeypatch.setattr(client_pool, "close_async_client", closeHttp)
    monkeypatch.setattr(LegacyCopyDispatch, "close", lambda: events.append("copy_closed"))
    monkeypatch.setattr(SyncJobWebhookOutbox, "close", lambda: events.append("outbox_closed"))
    monkeypatch.setattr(AsyncStoragePresigner, "closeCurrent", lambda: events.append("presigner_closed"))
    monkeypatch.setattr(apiModule, "dispose_all_engines", dispose)

    class ApplicationFailure(RuntimeError):
        pass

    async def exercise() -> None:
        async with apiModule.lifespan(SimpleNamespace(state=SimpleNamespace())):
            await asyncio.sleep(0)
            if doesApplicationFail:
                raise ApplicationFailure("application failed")

    if doesApplicationFail:
        with pytest.raises(ApplicationFailure):
            await exercise()
    else:
        await exercise()
    assert events == ["compiler_warmed", "flusher_stopped", "copy_closed", "outbox_closed", "presigner_closed", "native_closed", "http_closed", "database_disposed"]
