from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

configure_import_environment()
ensure_import_paths()


def _prepare_api_app_imports() -> None:
    api_root = str(Path(__file__).resolve().parents[2])
    if api_root in sys.path:
        sys.path.remove(api_root)
    sys.path.insert(0, api_root)


def _create_post_only_app() -> FastAPI:
    _prepare_api_app_imports()

    from app.core.exception_handlers import setup_exception_handlers

    app = FastAPI()

    @app.post("/v2/retrieval/query")
    async def query_retrieval() -> dict[str, bool]:
        return {"ok": True}

    setup_exception_handlers(app)
    return app


async def test_get_to_post_only_route_returns_method_not_allowed() -> None:
    app = _create_post_only_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v2/retrieval/query")

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    assert response_json["success"] is False
    assert error["code"] == "METHOD_NOT_ALLOWED"
    assert error["message"] == "Method not allowed"
