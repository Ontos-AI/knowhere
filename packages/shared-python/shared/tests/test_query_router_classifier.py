"""Unit tests for the retrieval query-mode classifier and public projection."""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest
from httpx import ConnectTimeout, UnsupportedProtocol

from shared.core.config import settings
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.routing.classifier import classify_query_mode
from shared.services.retrieval.routing.types import QueryMode


class _FakeClient:
    def __init__(self, content: str, usage: dict[str, int] | None = None) -> None:
        self.content = content
        self.usage = usage or {"total_tokens": 4}
        self.calls: list[dict[str, Any]] = []

    def chat_completion_with_usage(self, messages: object, **kwargs: object) -> tuple[str, dict[str, int]]:
        self.calls.append({"messages": messages, **kwargs})
        return self.content, self.usage


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.routing.config.QUERY_ROUTER_MODEL", "test-router-model"
    )
    monkeypatch.setattr(
        "shared.services.ai.openai_compatible_client_sync.get_openai_client",
        lambda **_kwargs: client,
    )


@pytest.mark.asyncio
async def test_classifier_parses_classic_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient('{"mode":"classic","reason":"single fact"}')
    _patch_client(monkeypatch, client)

    decision = await classify_query_mode("What is the design flood of dam A?")

    assert decision.mode is QueryMode.CLASSIC
    assert decision.source == "llm"
    assert decision.reason == "single fact"
    assert decision.model_name == "test-router-model"
    assert decision.model_config_source == "default"
    assert decision.failure_stage is None
    assert decision.tokens_used == 4
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["model"] == "test-router-model"
    assert client.calls[0]["usage_task"] == "retrieval.query_router"


@pytest.mark.asyncio
async def test_classifier_parses_agent_json_and_keeps_long_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "cross-document comparison of every listed project"
    client = _FakeClient(f'{{"mode":"agent","reason":"{reason}"}}')
    _patch_client(monkeypatch, client)

    decision = await classify_query_mode(
        "Compare the flood-control conclusions across these reports"
    )

    assert decision.mode is QueryMode.AGENT
    assert decision.source == "llm"
    assert decision.reason == reason


@pytest.mark.asyncio
async def test_classifier_invalid_mode_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient('{"mode":"hybrid","reason":"neither"}')
    _patch_client(monkeypatch, client)

    decision = await classify_query_mode("anything")

    assert decision.mode is QueryMode.AGENT
    assert decision.source == "fallback"
    assert decision.reason == ""
    assert decision.tokens_used == 4
    assert decision.failure_stage == "model_response"


@pytest.mark.asyncio
async def test_classifier_invalid_json_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient("not-json")
    _patch_client(monkeypatch, client)

    decision = await classify_query_mode("anything")

    assert decision.mode is QueryMode.AGENT
    assert decision.source == "fallback"
    assert decision.tokens_used == 4
    assert decision.failure_stage == "model_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "failure_stage"),
    [
        (TimeoutError("router timed out"), "model_request"),
        (ConnectTimeout("router connection timed out"), "model_connect"),
        (UnsupportedProtocol("unsupported scheme"), "model_protocol"),
    ],
)
async def test_classifier_exception_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch, error: Exception, failure_stage: str
) -> None:
    class _BoomClient:
        def chat_completion_with_usage(self, *_args: object, **_kwargs: object) -> tuple[str, dict[str, int]]:
            raise error

    _patch_client(monkeypatch, _BoomClient())

    decision = await classify_query_mode("anything")

    assert decision.mode is QueryMode.AGENT
    assert decision.source == "fallback"
    assert decision.model_name == "test-router-model"
    assert decision.tokens_used == 0
    assert decision.failure_stage == failure_stage
    assert decision.failure_error_type == type(error).__name__


@pytest.mark.asyncio
async def test_llm_mock_non_json_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LLM_MOCK_ENABLED", True)

    decision = await classify_query_mode("Find the value of X")

    assert decision.mode is QueryMode.AGENT
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_projection_passes_router_decision() -> None:
    public = await project_public_retrieval_response(
        {
            "namespace": "ns",
            "query": "q",
            "router_used": "classic_topk",
            "router_decision": {
                "mode": "classic",
                "reason": "single fact",
                "source": "llm",
                "model_name": "test-router-model",
                "tokens_used": 4,
            },
            "results": [],
        }
    )

    assert public["router_decision"] == {
        "mode": "classic",
        "reason": "single fact",
        "source": "llm",
        "model_name": "test-router-model",
        "tokens_used": 4,
    }


@pytest.mark.asyncio
async def test_projection_omits_router_decision_when_absent() -> None:
    public = await project_public_retrieval_response(
        {
            "namespace": "ns",
            "query": "q",
            "router_used": "small_corpus_all",
            "results": [],
        }
    )

    assert "router_decision" not in public
