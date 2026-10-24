from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
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
