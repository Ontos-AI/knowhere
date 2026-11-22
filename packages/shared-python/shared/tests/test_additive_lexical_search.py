"""Verify native namespace search sends no build roster."""

from shared.services.retrieval.lexical.searchplan import NativeSearchPlan


def test_additive_search_uses_scoped_native_filters_and_a_live_cursor() -> None:
    first = NativeSearchPlan.buildPayload(
        "contentTokens", buildIds=None, queryTokens=("heading",), limit=100,
        unitIds=None, extraFilters=({"term": {"documentId": "doc-allowed"}},),
    )
    assert first["query"] == {"bool": {
        "filter": [{"term": {"documentId": "doc-allowed"}}],
        "must": [{"match": {"contentTokens": {"query": "heading", "operator": "or"}}}],
    }}
    assert first["sort"][-1] == {"buildId": "asc"}
    nextPage = NativeSearchPlan.buildPayload(
        "contentTokens", buildIds=None, queryTokens=("heading",), limit=100,
        unitIds=None, searchAfter=(3.0, "doc-allowed", 0, "section", "attempt"),
    )
    assert nextPage["search_after"] == (3.0, "doc-allowed", 0, "section", "attempt")


import json
from uuid import uuid4

import httpx
import pytest

from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope


@pytest.mark.asyncio
async def test_namespace_search_refills_after_ineligible_records() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = body["query"]["bool"]
        assert not any("buildId" in str(field) for field in query["filter"])
        docs = [f"ineligible-{index}" for index in range(100)] if "search_after" not in body else ["eligible"]
        hits = [{
            "_id": doc, "_score": 1.0,
            "_source": {"documentId": doc, "revisionId": "rev", "buildId": "build", "unitId": doc, "sectionId": "section"},
            "sort": [1.0, doc, 0, doc, "build"],
        } for doc in docs]
        return httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": hits}})

    async def isEligible(candidates: tuple[tuple[str, str, str], ...]) -> set[tuple[str, str, str]]:
        return {candidate for candidate in candidates if candidate[0] == "eligible"}

    native = AsyncOpenSearchLexicalIndex(endpoint=f"http://127.0.0.1:{10000 + int(uuid4().hex[:4], 16) % 50000}",
        scope=LexicalScope(ownerId="publisher", namespace="additive-refill"))
    async with httpx.AsyncClient(base_url="http://native.invalid", transport=httpx.MockTransport(respond)) as client:
        native._client = client
        hits = await native.searchNamespace(queryTokens=("needle",), limit=1, filterCandidates=isEligible)
    assert [hit.unit.documentId for hit in hits] == ["eligible"]
    assert hits[0].pathRank == hits[0].contentRank == 0


def test_document_cleanup_deletes_all_builds_by_exact_document_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    native = OpenSearchLexicalIndex(endpoint="http://127.0.0.1:65191",
        scope=LexicalScope(ownerId="publisher", namespace="archival-neighbors"))
    requests: list[tuple[str, object]] = []
    records = {"old-baseline": "archived", "current-fine": "archived", "neighbor": "archived-neighbor"}

    def respond(method: str, path: str, *, payload: dict) -> object:
        requests.append((path, payload))
        match = payload["query"]["term"]["documentId"]
        if "_delete_by_query" in path:
            for record in [record for record, document_id in records.items() if document_id == match]:
                records.pop(record)
            return {"timed_out": False, "version_conflicts": 0, "failures": []}
        return {"count": sum(document_id == match for document_id in records.values()), "_shards": {"failed": 0}}

    monkeypatch.setattr(native, "_request", respond)
    assert native.deleteDocument("archived")
    assert records == {"neighbor": "archived-neighbor"}
    assert len(requests) == 2
