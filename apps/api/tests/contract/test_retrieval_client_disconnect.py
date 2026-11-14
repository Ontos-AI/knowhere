"""Exercise client disconnect at the v2 retrieval HTTP boundary."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.api.v2.routes import retrieval
from shared.core.database import get_db


def make_api() -> FastAPI:
    app = FastAPI()
    app.include_router(retrieval.router, prefix="/api/v2/retrieval")
    app.dependency_overrides[retrieval.with_current_user] = lambda: SimpleNamespace(
        user_id="test-user"
    )
    app.dependency_overrides[get_db] = object
    return app


def request_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/api/v2/retrieval/query",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }


def request_body() -> dict[str, Any]:
    return {
        "type": "http.request",
        "body": json.dumps(
            {"namespace": "client-disconnect", "query": "topic"}
        ).encode(),
        "more_body": False,
    }


@pytest.mark.asyncio
async def test_v2_disconnect_cancels_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    request_messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await request_messages.put(request_body())
    response_messages: list[dict[str, Any]] = []

    async def retrieve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("the disconnected query must be cancelled")

    async def send(message: dict[str, Any]) -> None:
        response_messages.append(message)

    monkeypatch.setattr(retrieval, "execute_retrieval_query", retrieve)
    app = make_api()
    request_task = asyncio.create_task(app(request_scope(), request_messages.get, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await request_messages.put({"type": "http.disconnect"})
        await asyncio.wait_for(asyncio.shield(request_task), timeout=1)
        assert cancelled.is_set()
        assert response_messages[0]["status"] == 499
    finally:
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_v2_normal_result_does_not_wait_for_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await request_messages.put(request_body())
    response_messages: list[dict[str, Any]] = []

    async def retrieve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "namespace": "client-disconnect",
            "query": "topic",
            "router_used": "classic",
            "results": [],
        }

    async def send(message: dict[str, Any]) -> None:
        response_messages.append(message)

    monkeypatch.setattr(retrieval, "execute_retrieval_query", retrieve)
    app = make_api()
    await asyncio.wait_for(app(request_scope(), request_messages.get, send), timeout=1)
    assert response_messages[0]["status"] == 200
    assert json.loads(response_messages[-1]["body"])["results"] == []
