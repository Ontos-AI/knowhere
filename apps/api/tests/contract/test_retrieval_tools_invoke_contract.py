from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

configure_import_environment()
ensure_import_paths()


def _create_invoke_app() -> FastAPI:
    from app.api.dependencies.current_user import with_current_user
    from app.api.v2.routes.retrieval import router
    from app.services.rate_limit.data_structures import CurrentUser

    from shared.core.database import get_db

    app = FastAPI()
    app.include_router(router, prefix="/api/v2/retrieval")

    async def override_user():
        yield CurrentUser(user_id="user_invoke", user_tier="paid")

    async def override_db():
        yield MagicMock()

    app.dependency_overrides[with_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return app


async def test_invoke_rejects_tools_outside_the_hipo_whitelist() -> None:
    transport = ASGITransport(app=_create_invoke_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v2/retrieval/tools/invoke",
            json={"tool": "corpus.pick", "arguments": {}},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "tool is not supported"}


async def test_invoke_dispatches_whitelisted_tool_with_namespace_header(
    monkeypatch: Any,
) -> None:
    from app.api.v2.routes import retrieval as retrieval_routes

    from shared.services.retrieval.agent_tools.registry import ToolResult

    captured: dict[str, object] = {}

    async def fake_dispatch(name: str, ctx: object, args: dict[str, Any]) -> ToolResult:
        captured["tool"] = name
        captured["namespace"] = getattr(ctx, "namespace")
        captured["user_id"] = getattr(ctx, "user_id")
        captured["arguments"] = args
        return ToolResult(
            text="ok",
            payload={"document_id": "doc-1"},
            refs=[{"document_id": "doc-1"}],
            error=None,
        )

    monkeypatch.setattr(retrieval_routes.REGISTRY, "dispatch", fake_dispatch)

    transport = ASGITransport(app=_create_invoke_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v2/retrieval/tools/invoke",
            headers={"x-knowhere-namespace": "brain-ns"},
            json={
                "tool": "corpus.recall",
                "arguments": {"query": "revenue", "scope": [{"document_id": "doc-1"}]},
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "text": "ok",
        "payload": {"document_id": "doc-1"},
        "refs": [{"document_id": "doc-1"}],
        "error": None,
    }
    assert captured == {
        "tool": "corpus.recall",
        "namespace": "brain-ns",
        "user_id": "user_invoke",
        "arguments": {"query": "revenue", "scope": [{"document_id": "doc-1"}]},
    }


async def test_invoke_defaults_namespace_when_header_is_omitted(
    monkeypatch: Any,
) -> None:
    from app.api.v2.routes import retrieval as retrieval_routes

    from shared.services.retrieval.agent_tools.registry import ToolResult

    captured: dict[str, object] = {}

    async def fake_dispatch(name: str, ctx: object, args: dict[str, Any]) -> ToolResult:
        del name, args
        captured["namespace"] = getattr(ctx, "namespace")
        return ToolResult(text="", payload={}, refs=[], error=None)

    monkeypatch.setattr(retrieval_routes.REGISTRY, "dispatch", fake_dispatch)

    transport = ASGITransport(app=_create_invoke_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v2/retrieval/tools/invoke",
            json={"tool": "corpus.assets", "arguments": {}},
        )

    assert response.status_code == 200
    assert captured["namespace"] == "default"
    assert cast(dict[str, object], response.json())["error"] is None
