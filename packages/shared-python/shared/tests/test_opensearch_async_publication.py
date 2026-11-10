"""Manual retries await native publication without a synchronous HTTP client."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.unit import LexicalUnit


@pytest.mark.asyncio
async def test_async_publication_uses_byte_bounded_bulk_and_checks_visibility() -> None:
    bodies: list[bytes] = []
    records: dict[str, dict[str, object]] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_count"):
            return httpx.Response(200, json={"count": len(records), "_shards": {"failed": 0}})
        assert request.url.path.endswith("/_bulk") and request.url.params["refresh"] == "wait_for"
        bodies.append(request.content)
        lines: list[str] = request.content.decode().splitlines()
        items: list[dict[str, dict[str, int]]] = []
        for position in range(0, len(lines), 2):
            action: dict = json.loads(lines[position])
            records[action["index"]["_id"]] = json.loads(lines[position + 1])
            items.append({"index": {"status": 201}})
        return httpx.Response(200, json={"errors": False, "items": items})

    native: AsyncOpenSearchLexicalIndex = AsyncOpenSearchLexicalIndex(
        endpoint=f"http://127.0.0.1:{10000 + int(uuid4().hex[:4], 16) % 50000}", scope=LexicalScope(ownerId="async-bulk", namespace="bulk"),
        maximumBulkBytes=800)
    async with httpx.AsyncClient(base_url="http://native.invalid", transport=httpx.MockTransport(respond)) as client:
        native._client = client
        units: tuple[LexicalUnit, ...] = tuple(LexicalUnit(documentId="document", revisionId="revision", unitId=str(position),
            sectionId="section", contentTokens="验收计划" * 30) for position in range(3))
        await native.writeBuild(buildId="build", units=units)
        assert len(bodies) == 3 and all(len(body) <= 800 for body in bodies)
        assert len(records) == 3
        native.close()


@pytest.mark.asyncio
async def test_async_later_bulk_failure_does_not_retry_or_check_partial_visibility() -> None:
    calls: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith("/_bulk")
        if len(calls) == 2:
            return httpx.Response(503, json={"error": {"type": "unavailable"}})
        return httpx.Response(200, json={"errors": False, "items": [{"index": {"status": 201}}]})

    native: AsyncOpenSearchLexicalIndex = AsyncOpenSearchLexicalIndex(
        endpoint=f"http://127.0.0.1:{10000 + int(uuid4().hex[:4], 16) % 50000}", scope=LexicalScope(ownerId="async", namespace="failure"),
        maximumBulkBytes=800)
    async with httpx.AsyncClient(base_url="http://native.invalid", transport=httpx.MockTransport(respond)) as client:
        native._client = client
        units: tuple[LexicalUnit, ...] = tuple(LexicalUnit(documentId="document", revisionId="revision", unitId=str(position),
            sectionId="section", contentTokens="验收计划" * 30) for position in range(3))
        with pytest.raises(LexicalIndexError, match="HTTP 503"):
            await native.writeBuild(buildId="build", units=units)
        assert len(calls) == 2
        native.close()
