"""Retrieval usage persist writes model-call tokens and duration."""

from __future__ import annotations

import os
from typing import Any, cast

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.ai.retrieval_ledger import (
    cleanup_retrieval_ledger,
    init_retrieval_ledger,
    record_retrieval_call,
)
from shared.services.retrieval.execution.route_types import RetrievalRouteContext
from shared.services.retrieval.execution.routes import _persist_retrieval_usage_run


class _Recorder:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.model_calls: list[object] = []
        self.complete_kwargs: dict[str, Any] = {}

    async def create_run(self) -> None:
        return None

    def record_decision_trace_step(self, _step: object) -> None:
        return None

    def record_budget_stop(self, _reason: str) -> None:
        return None

    def record_model_call(self, entry: object) -> None:
        self.model_calls.append(entry)

    async def complete(self, *_args: object, **kwargs: object) -> None:
        self.complete_kwargs = dict(kwargs)


@pytest.mark.asyncio
async def test_persist_writes_model_steps_and_query_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    captured: dict[str, Any] = {}

    def _factory(*args: object, **kwargs: object) -> _Recorder:
        del args
        captured.update(kwargs)
        return recorder

    monkeypatch.setattr(
        "shared.services.retrieval.trace.TraceRecorder",
        _factory,
    )
    init_retrieval_ledger()
    try:
        record_retrieval_call(
            {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            model="router-model",
            task="retrieval.query_router",
            duration_ms=11,
        )
        record_retrieval_call(
            {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
            model="embed-model",
            task="retrieval.query_embed",
            duration_ms=7,
        )
        context = RetrievalRouteContext(
            db=cast(AsyncSession, object()),
            user_id="u1",
            namespace="ns",
            query="q",
            top_k=3,
            exclude_document_ids=[],
            exclude_sections=[],
            allowed_chunk_types=None,
            chunk_types=None,
            signal_paths=None,
            filter_mode="delete",
            rerank=False,
            threshold=0.0,
            internal_recall_k=None,
            effective_recall_k=3,
            use_agentic=False,
        )
        await _persist_retrieval_usage_run(
            db=cast(AsyncSession, object()),
            context=context,
            router_used="classic_topk",
            ranked_rows=[{"document_id": "doc_1"}],
        )
        assert len(recorder.model_calls) == 2
        assert recorder.complete_kwargs["token_count"] == 18
        assert recorder.complete_kwargs["latency_ms"] >= 0
        assert captured["agentic_enabled"] is False
    finally:
        cleanup_retrieval_ledger()


@pytest.mark.asyncio
async def test_persist_marks_agent_explore_as_agentic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _factory(*args: object, **kwargs: object) -> _Recorder:
        del args
        captured.update(kwargs)
        return _Recorder()

    monkeypatch.setattr(
        "shared.services.retrieval.trace.TraceRecorder",
        _factory,
    )
    init_retrieval_ledger()
    try:
        context = RetrievalRouteContext(
            db=cast(AsyncSession, object()),
            user_id="u1",
            namespace="ns",
            query="q",
            top_k=3,
            exclude_document_ids=[],
            exclude_sections=[],
            allowed_chunk_types=None,
            chunk_types=None,
            signal_paths=None,
            filter_mode="delete",
            rerank=False,
            threshold=0.0,
            internal_recall_k=None,
            effective_recall_k=3,
            use_agentic=True,
        )
        await _persist_retrieval_usage_run(
            db=cast(AsyncSession, object()),
            context=context,
            router_used="agent_explore",
            ranked_rows=[],
        )
        assert captured["agentic_enabled"] is True
    finally:
        cleanup_retrieval_ledger()


@pytest.mark.asyncio
async def test_persist_skips_when_ledger_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    def _factory(*args: object, **kwargs: object) -> _Recorder:
        del args, kwargs
        created.append(object())
        return _Recorder()

    monkeypatch.setattr(
        "shared.services.retrieval.trace.TraceRecorder",
        _factory,
    )
    context = RetrievalRouteContext(
        db=cast(AsyncSession, object()),
        user_id="u1",
        namespace="ns",
        query="q",
        top_k=3,
        exclude_document_ids=[],
        exclude_sections=[],
        allowed_chunk_types=None,
        chunk_types=None,
        signal_paths=None,
        filter_mode="delete",
        rerank=False,
        threshold=0.0,
        internal_recall_k=None,
        effective_recall_k=3,
        use_agentic=False,
    )
    await _persist_retrieval_usage_run(
        db=cast(AsyncSession, object()),
        context=context,
        router_used="agent_explore",
        ranked_rows=[],
    )
    assert created == []


def test_cursor_missing_usage_does_not_become_zero() -> None:
    init_retrieval_ledger()
    try:
        record_retrieval_call(
            None,
            model="composer",
            task="retrieval.agent_explore",
            duration_ms=40,
        )
        from shared.services.ai.retrieval_ledger import get_current_retrieval_ledger

        ledger = get_current_retrieval_ledger()
        assert ledger is not None
        assert ledger.total_tokens_or_none() is None
        assert ledger.entries[0].duration_ms == 40
    finally:
        cleanup_retrieval_ledger()
