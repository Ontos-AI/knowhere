"""Lifecycle uncertainty retains the original execution deadline classification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
import time
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-lifecycle-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.asynccoordination import AsyncLexicalCoordination
from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.coordination import _Operation
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.scope import LexicalScope


@pytest.mark.asyncio
@pytest.mark.parametrize("doesSettlementFail", [False, True])
@pytest.mark.parametrize("isLexicalTimeout", [False, True])
async def test_lifecycle_cleanup_preserves_original_timeout(
    doesSettlementFail: bool, isLexicalTimeout: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    error: Exception = (
        LexicalIndexError(
            "Original execution expired",
            isRetryable=True,
            hasUnknownOutcome=True,
            isTimeout=True,
        )
        if isLexicalTimeout
        else TimeoutError("Original execution expired")
    )
    coordination, native, operation = createExecution(
        error, doesSettlementFail, monkeypatch
    )
    with pytest.raises(LexicalIndexError) as captured:
        await coordination._execute(native, operation, deadline=time.monotonic() + 1)
    assert captured.value.isTimeout
    assert captured.value.isRetryable and captured.value.hasUnknownOutcome
    native.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("doesSettlementFail", [False, True])
async def test_lifecycle_cleanup_preserves_cancellation(
    doesSettlementFail: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    error: asyncio.CancelledError = asyncio.CancelledError("Original request cancelled")
    coordination, native, operation = createExecution(
        error, doesSettlementFail, monkeypatch
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        await coordination._execute(native, operation, deadline=time.monotonic() + 1)
    assert captured.value is error
    native.close.assert_called_once()


def createExecution(
    error: BaseException, doesSettlementFail: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[AsyncLexicalCoordination, MagicMock, _Operation]:
    async def invoke(operation: Callable[[], object]) -> object:
        return operation()

    database: MagicMock = MagicMock(spec=AsyncLexicalDatabase)
    database.sessions = MagicMock()
    database.run = AsyncMock(side_effect=invoke)
    coordination: AsyncLexicalCoordination = AsyncLexicalCoordination(
        database, executionDeadline=IndexingDeadline(1)
    )

    def finish(*arguments: object, **options: object) -> None:
        if doesSettlementFail:
            raise RuntimeError("SQL settlement unavailable")

    monkeypatch.setattr(coordination.decisions, "_finish", finish)
    native: MagicMock = MagicMock(spec=AsyncOpenSearchLexicalIndex)
    native.indexName = "index"
    native.forScope.return_value = native
    native.getLifecycleSnapshot = AsyncMock(side_effect=error)
    scope: LexicalScope = LexicalScope(ownerId="owner", namespace="namespace")
    operation: _Operation = _Operation(
        scope, "operation", "index", "uuid", "incarnation", "cluster", 1, "open"
    )
    return coordination, native, operation
