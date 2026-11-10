"""Sync lifecycle uncertainty retains execution timeout classification."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-lifecycle-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.lexical.coordination import (
    LexicalCoordination,
    _Operation,
)
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


@pytest.mark.parametrize("doesSettlementFail", [False, True])
@pytest.mark.parametrize("isLexicalTimeout", [False, True])
def test_sync_lifecycle_cleanup_preserves_original_timeout(
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
    coordination: LexicalCoordination = LexicalCoordination(MagicMock())

    def finish(*arguments: object, **options: object) -> None:
        if doesSettlementFail:
            raise RuntimeError("SQL settlement unavailable")

    monkeypatch.setattr(coordination, "_finish", finish)
    native: MagicMock = MagicMock(spec=OpenSearchLexicalIndex)
    native.indexName = "index"
    native.forScope.return_value = native
    native.getLifecycleSnapshot.side_effect = error
    scope: LexicalScope = LexicalScope(ownerId="owner", namespace="namespace")
    operation: _Operation = _Operation(
        scope, "operation", "index", "uuid", "incarnation", "cluster", 1, "open"
    )
    with pytest.raises(LexicalIndexError) as captured:
        coordination._execute(native, operation, deadline=time.monotonic() + 1)
    assert captured.value.isTimeout
    assert captured.value.isRetryable and captured.value.hasUnknownOutcome
    native.close.assert_called_once()
