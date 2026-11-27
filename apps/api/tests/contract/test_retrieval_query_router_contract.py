from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, NoReturn, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient, ConnectError
from loguru import logger
from pytest import MonkeyPatch

from shared.services.retrieval.routing.types import QueryMode, RouterDecision
from tests.contract.test_retrieval_classic_map_unit_contract import _publish_document
from tests.contract.test_retrieval_contract import (
    _episode_keeping_refs,
    _patch_harness,
    _seed_retrieval_document,
)


def _decision(*, mode: QueryMode, source: str = "llm") -> RouterDecision:
    return RouterDecision(
        mode=mode,
        reason="contract",
        source=source,
        model_name="test-router-model",
        tokens_used=2,
    )


async def _publish_above_top_k(*, namespace: str) -> dict[str, str]:
    identifier = uuid4().hex[:8]
    chunk_id = f"hit-{identifier}"
    target = await _publish_document(
        namespace=namespace,
        source_file_name="target.pdf",
        chunks=[
            {
                "chunk_id": chunk_id,
                "type": "text",
                "content": "unique alpha ranking marker",
                "path": "target.pdf/Root/Hit/body",
                "order": 1,
                "metadata": {},
            }
        ],
    )
    await _publish_document(
        namespace=namespace,
        source_file_name="filler.pdf",
        chunks=[
            {
                "chunk_id": f"filler-{identifier}",
                "type": "text",
                "content": "unrelated filler content",
                "path": "filler.pdf/Root/Noise/body",
                "order": 1,
                "metadata": {},
            }
        ],
    )
    return {
        **target,
        "chunk_id": chunk_id,
        "section_path": "Root / Hit / body",
    }


def _patch_classifier(
    monkeypatch: MonkeyPatch,
    decision: RouterDecision,
    calls: list[str],
) -> None:
    from shared.services.retrieval.execution import routes as retrieval_routes

    async def fake_classify(query: str) -> RouterDecision:
        calls.append(query)
        return decision

    monkeypatch.setattr(retrieval_routes, "classify_query_mode", fake_classify)


def _patch_classifier_boom(monkeypatch: MonkeyPatch, calls: list[str], message: str) -> None:
    from shared.services.retrieval.execution import routes as retrieval_routes

    async def boom(_query: str) -> RouterDecision:
        calls.append("called")
        raise AssertionError(message)

    monkeypatch.setattr(retrieval_routes, "classify_query_mode", boom)


@pytest.mark.asyncio
async def test_unset_use_agentic_classic_decision_uses_classic_route(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    namespace = f"router-classic-{uuid4().hex[:8]}"
    calls: list[str] = []

    async with developer_api_client_factory() as api_client:
        await _publish_above_top_k(namespace=namespace)
        _patch_classifier(monkeypatch, _decision(mode=QueryMode.CLASSIC), calls)
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert calls == ["unique alpha ranking"]
    assert body["router_used"] == "classic_topk"
    assert body["router_decision"] == {
        "mode": "classic",
        "reason": "contract",
        "source": "llm",
        "model_name": "test-router-model",
        "tokens_used": 2,
    }


@pytest.mark.asyncio
async def test_unset_use_agentic_agent_decision_uses_agent_explore(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    namespace = f"router-agent-{uuid4().hex[:8]}"
    calls: list[str] = []

    async with developer_api_client_factory() as api_client:
        target = await _publish_above_top_k(namespace=namespace)
        _patch_classifier(monkeypatch, _decision(mode=QueryMode.AGENT), calls)
        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(
                documents=[target],
                notes="unique alpha ranking marker",
            ),
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert calls == ["unique alpha ranking"]
    assert body["router_used"] == "agent_explore"
    assert body["router_decision"]["mode"] == "agent"
    assert body["router_decision"]["source"] == "llm"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uses_override", "fallback_succeeds"),
    [(True, True), (False, False)],
    ids=["request-model-fallback-succeeds", "default-model-fallback-fails"],
)
async def test_automatic_router_logs_model_failure_and_fallback_outcome(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
    uses_override: bool,
    fallback_succeeds: bool,
) -> None:
    namespace: str = f"router-fallback-{uuid4().hex[:8]}"
    request_key: str = "test-router-provider-secret"
    query: str = "private test query text"
    events: list[dict[str, object]] = []
    payload: dict[str, object] = {"namespace": namespace, "query": query, "top_k": 1}
    if uses_override:
        payload["llm_config"] = {
            "model": "request-router-model",
            "api_key": request_key,
            "base_url": "https://router.example.test/v1",
        }

    async with developer_api_client_factory() as api_client:
        from shared.core.exceptions.domain_exceptions import (
            LLMServiceException,
            UnavailableException,
        )
        from shared.services.ai import openai_compatible_client_sync
        from shared.services.retrieval.agent_explore import harness
        from shared.services.retrieval.routing import config

        class FailingRouterClient:
            def chat_completion_with_usage(
                self, *_args: object, **_kwargs: object
            ) -> NoReturn:
                connection_error: ConnectError = ConnectError("Provider connection failed")
                connection_error.__cause__ = OSError(111, "Connection refused")
                raise LLMServiceException(
                    internal_message="Provider request failed",
                    original_exception=connection_error,
                ) from connection_error

        class FailingAgent:
            async def run_episode(self, **_kwargs: object) -> NoReturn:
                raise UnavailableException(
                    internal_message="Agent unavailable", retry_after=1
                )

        target: dict[str, str] = await _publish_above_top_k(namespace=namespace)
        monkeypatch.setattr(config, "QUERY_ROUTER_MODEL", "default-router-model")
        monkeypatch.setattr(
            openai_compatible_client_sync,
            "get_openai_client",
            lambda **_kwargs: FailingRouterClient(),
        )
        if fallback_succeeds:
            _patch_harness(monkeypatch, _episode_keeping_refs(documents=[target]))
        else:
            monkeypatch.setattr(
                harness, "resolve_harness", lambda **_kwargs: FailingAgent()
            )

        sink_id: int = logger.add(
            lambda message: events.append(message.record["extra"].copy()),
            level="INFO",
            filter=lambda record: str(record["extra"].get("event", "")).startswith(
                "retrieval.query_router."
            ),
        )
        try:
            response = await api_client.post("/api/v2/retrieval/query", json=payload)
        finally:
            logger.remove(sink_id)

    assert response.status_code == (200 if fallback_succeeds else 503)
    assert [entry["event"] for entry in events] == [
        "retrieval.query_router.decision",
        "retrieval.query_router.fallback",
    ]
    decision, fallback = events
    request_id: str = response.headers["x-request-id"]
    assert decision["request_id"] == fallback["request_id"] == request_id
    assert decision["routing_trigger"] == "use_agentic_unset"
    assert decision["model_config_source"] == ("request" if uses_override else "default")
    assert decision["model_name"] == (
        "request-router-model" if uses_override else "default-router-model"
    )
    assert decision["failure_stage"] == "model_connect"
    assert decision["failure_error_type"] == "ConnectError"
    assert decision["failure_errno"] == 111
    assert fallback["fallback_outcome"] == (
        "succeeded" if fallback_succeeds else "failed"
    )
    if fallback_succeeds:
        assert fallback["agent_stop_reason"] == "finished"
        assert response.json()["router_decision"] == {
            "mode": "agent",
            "reason": "",
            "source": "fallback",
            "model_name": "request-router-model",
            "tokens_used": 0,
        }
    else:
        assert fallback["fallback_error_type"] == "UnavailableException"
    assert request_key not in str(events)
    assert query not in str(events)
    assert "router.example.test" not in str(events)


@pytest.mark.asyncio
async def test_explicit_use_agentic_does_not_call_classifier(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    namespace = f"router-explicit-{uuid4().hex[:8]}"
    calls: list[str] = []

    async with developer_api_client_factory() as api_client:
        target = await _publish_above_top_k(namespace=namespace)
        _patch_classifier_boom(
            monkeypatch,
            calls,
            "classifier must not run when use_agentic is set",
        )
        classic = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
                "use_agentic": False,
            },
        )
        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(documents=[target], notes="explicit agent"),
        )
        agent = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
                "use_agentic": True,
            },
        )

    assert classic.status_code == 200
    assert agent.status_code == 200
    classic_body = cast(dict[str, Any], classic.json())
    agent_body = cast(dict[str, Any], agent.json())
    assert calls == []
    assert classic_body["router_used"] == "classic_topk"
    assert classic_body.get("router_decision") is None
    assert agent_body["router_used"] == "agent_explore"
    assert agent_body.get("router_decision") is None


@pytest.mark.asyncio
async def test_disabled_router_keeps_unset_use_agentic_on_agent(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    namespace = f"router-disabled-{uuid4().hex[:8]}"
    calls: list[str] = []

    async with developer_api_client_factory() as api_client:
        target = await _publish_above_top_k(namespace=namespace)
        monkeypatch.setattr(
            "shared.services.retrieval.routing.config.QUERY_ROUTER_ENABLED",
            False,
        )
        _patch_classifier_boom(
            monkeypatch,
            calls,
            "classifier must not run when the router is disabled",
        )
        _patch_harness(
            monkeypatch,
            _episode_keeping_refs(documents=[target], notes="disabled router"),
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert calls == []
    assert body["router_used"] == "agent_explore"
    assert body["router_decision"] == {
        "mode": "agent",
        "reason": "",
        "source": "disabled",
        "model_name": "",
        "tokens_used": 0,
    }


@pytest.mark.asyncio
async def test_small_corpus_skips_query_router(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    namespace = f"router-small-{uuid4().hex[:8]}"
    calls: list[str] = []

    async with developer_api_client_factory() as api_client:
        await _seed_retrieval_document(
            user_id="local-dev-user",
            namespace=namespace,
            source_file_name="only.pdf",
            section_path="only/section",
            content="only chunk",
        )
        _patch_classifier_boom(
            monkeypatch,
            calls,
            "classifier must not run on the small-corpus path",
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "only chunk",
                "top_k": 10,
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert calls == []
    assert body["router_used"] == "small_corpus_all"
    assert body.get("router_decision") is None
