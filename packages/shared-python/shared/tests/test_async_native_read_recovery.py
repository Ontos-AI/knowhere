"""No-service checks for the one permitted closed-index recovery."""

from __future__ import annotations

import os
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-kw04-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.recency import LexicalRecency


@pytest.mark.asyncio
@pytest.mark.parametrize("doesSecondSearchFail", [False, True])
async def test_async_read_recovers_closed_index_only_once_on_original_bindings(monkeypatch: pytest.MonkeyPatch, doesSecondSearchFail: bool) -> None:
    engine = create_async_engine("postgresql+asyncpg://test:test@localhost/test")
    reader: AsyncPublishedLexicalRead = AsyncPublishedLexicalRead(userId="user", namespace="ns", bind=engine)
    reader._hasStarted = True
    reader._indexUuid = "captured-index"
    reader.pins = RetrievalRevisionPins({"doc": "rev"}, generation=7)
    reader.builds = MappingProxyType({"doc": "build"})
    reader.epoch = 9
    closed: LexicalIndexError = LexicalIndexError("closed", isIndexClosed=True)
    second: object = LexicalIndexError("still closed", isIndexClosed=True) if doesSecondSearchFail else ()
    search: AsyncMock = AsyncMock(side_effect=[closed, second])
    native = SimpleNamespace(search=search, close=lambda: None)
    ensure: AsyncMock = AsyncMock(return_value=("captured-index", 9))
    reader._asyncNative = native  # type: ignore[assignment]
    reader._asyncCoordination = SimpleNamespace(ensureIndex=ensure)  # type: ignore[assignment]
    monkeypatch.setattr(LexicalRecency, "recordAccess", lambda identity: None)
    try:
        if doesSecondSearchFail:
            with pytest.raises(LexicalIndexError) as caught:
                await reader.searchAsync(buildIds=("build",), queryTokens=("query",), limit=2, unitIds=("unit",))
            assert caught.value is second
        else:
            assert await reader.searchAsync(buildIds=("build",), queryTokens=("query",), limit=2, unitIds=("unit",)) == ()
        assert ensure.await_count == 1
        assert search.await_count == 2
        assert search.await_args_list[0] == search.await_args_list[1]
        assert dict(reader.pins) == {"doc": "rev"}
        assert dict(reader.builds) == {"doc": "build"}
        assert reader.pins.generation == 7
        assert reader.epoch == 9
    finally:
        await reader.closeAsync()
        await engine.dispose()
