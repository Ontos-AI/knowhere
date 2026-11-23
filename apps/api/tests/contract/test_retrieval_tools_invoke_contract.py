from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from importlib import import_module
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

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


async def test_invoke_recall_returns_table_connected_to_additive_body(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import update

    from app.api.v2.routes.retrieval import RetrievalToolInvokeResponse

    from shared.models.database.document import Document
    from shared.services.retrieval.lexical.additivevisibility import AdditiveVisibility
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult
    from shared.testing.contract_runtime import CONTRACT_DEVELOPER_USER_ID
    from tests.contract.test_retrieval_classic_map_unit_contract import _publish_document
    from tests.support.retrieval_snapshot_support import contract_db_session

    namespace: str = f"recall-assets-{uuid4().hex[:8]}"
    build_id: str = "recall-baseline"
    body_id: str = "revenue-body"
    table_id: str = "revenue-table"
    body_text: str = "Revenue by quarter"
    table_html: str = "<table><tr><th>Quarter</th><th>Revenue</th></tr><tr><td>Q1</td><td>42</td></tr></table>"

    async with developer_api_client_factory() as api_client:
        published: dict[str, str] = await _publish_document(
            namespace=namespace,
            source_file_name="revenue.pdf",
            chunks=[
                {
                    "chunk_id": body_id, "type": "text", "content": body_text,
                    "path": "revenue.pdf/Root/Revenue/body", "order": 1,
                    "metadata": {"connect_to": [{"target": table_id}]},
                },
                {
                    "chunk_id": table_id, "type": "table", "content": table_html,
                    "path": "revenue.pdf/Root/table", "order": 2,
                },
            ],
        )
        document_id: str = published["document_id"]
        revision_id: str = published["job_result_id"]
        async with contract_db_session() as db:
            await db.execute(
                update(Document).where(Document.document_id == document_id)
                .values(current_indexing_build_id=build_id)
            )
            await db.commit()
            hits: list[dict[str, Any]] = await AdditiveVisibility.loadRows(
                db,
                scope=LexicalScope(ownerId=CONTRACT_DEVELOPER_USER_ID, namespace=namespace),
                chunkTargets=((document_id, revision_id, build_id, body_id),),
            )
        assert [hit["chunk_id"] for hit in hits] == [body_id]

        async def find_fixture_body(*_args: object, **_kwargs: object) -> DiscoveryResult:
            return DiscoveryResult(status="ok", payload={"fused_rows": hits})

        recall_module: ModuleType = import_module(
            "shared.services.retrieval.agent_tools.tools.recall"
        )
        monkeypatch.setattr(recall_module, "hybrid_discovery", find_fixture_body)
        app: FastAPI = import_module("main").app
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url=str(api_client.base_url),
            headers=api_client.headers,
        ) as client:
            response: Response = await client.post(
                "/api/v2/retrieval/tools/invoke",
                headers={"X-Knowhere-Namespace": namespace},
                json={"tool": "corpus.recall", "arguments": {"query": "quarterly revenue"}},
            )

    assert response.status_code == 200
    result: RetrievalToolInvokeResponse = RetrievalToolInvokeResponse.model_validate(
        response.json()
    )
    assert result.error is None
    assert result.refs == [{"document_id": document_id, "chunk_id": body_id}]
    assert result.payload["rows"][0]["mounted_chunk_ids"] == [table_id]
    assert body_text in result.text
    assert table_html in result.text
