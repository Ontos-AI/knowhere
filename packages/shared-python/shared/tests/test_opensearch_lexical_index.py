"""Exercise native lexical visibility on an explicitly configured test node."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import os
import json
from uuid import uuid4

import httpx
import pytest

from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.unit import LexicalUnit


def test_written_build_is_search_visible_without_a_manual_refresh() -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")

    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit

    scope: LexicalScope = LexicalScope(
        ownerId="integration-test",
        namespace=f"visibility-{uuid4().hex}",
    )
    index: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
        endpoint=endpoint,
        scope=scope,
        indexPrefix="knowhere-slice-test",
    )
    try:
        index.createIndex()
        index.writeBuild(
            buildId="build-first",
            units=(
                LexicalUnit(
                    documentId="document-first",
                    revisionId="revision-first",
                    unitId="unit-first",
                    sectionId="section-first",
                    pathTokens="工程 项目",
                    contentTokens="工程 验收 计划",
                ),
            ),
        )
        hits = index.search(buildIds=("build-first",), queryTokens=("工程",), limit=20)
        assert len(hits) == 1
        assert hits[0].unit.unitId == "unit-first"
        assert hits[0].unit.revisionId == "revision-first"
        assert hits[0].buildId == "build-first"
        assert hits[0].score == pytest.approx(3 / 61)
    finally:
        response: httpx.Response = httpx.delete(
            f"{endpoint}/{index.indexName}", timeout=15, trust_env=False
        )
        assert response.status_code in (200, 404)
        index.close()


def test_lost_writer_authority_stops_before_the_next_bulk() -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="integration-test", namespace=f"stale-{uuid4().hex}"),
        indexPrefix="knowhere-slice-test", maximumBulkBytes=800)
    checks: int = 0

    def assertAuthority() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise LexicalIndexError("Writer ownership expired")

    try:
        index.createIndex()
        units = tuple(LexicalUnit(documentId="document-first", revisionId="revision-first",
            unitId=f"unit-{position}", sectionId="section-first", contentTokens="工程")
            for position in range(201))
        with pytest.raises(LexicalIndexError, match="ownership expired"):
            index.writeBuild(buildId="stale-build", units=units, beforeBatch=assertAuthority)
        response = httpx.post(f"{endpoint}/{index.indexName}/_count",
            json={"query": {"term": {"buildId": "stale-build"}}}, timeout=15, trust_env=False)
        response.raise_for_status()
        assert 0 < response.json()["count"] < len(units)
    finally:
        assert httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False).status_code in (200, 404)
        index.close()


def test_http_success_with_a_failed_bulk_item_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="integration-test", namespace=f"partial-{uuid4().hex}"),
        indexPrefix="knowhere-slice-test")
    request = OpenSearchLexicalIndex._request

    def corruptSecondItem(self: OpenSearchLexicalIndex, method: str, path: str, *,
                          payload: object | None = None, textBody: str | None = None) -> object:
        if "_bulk" in path and textBody is not None:
            lines: list[str] = textBody.splitlines()
            document: dict[str, object] = json.loads(lines[3])
            document["unexpectedField"] = "rejected by the strict native mapping"
            lines[3] = json.dumps(document)
            textBody = "\n".join(lines) + "\n"
        return request(self, method, path, payload=payload, textBody=textBody)

    try:
        native.createIndex()
        monkeypatch.setattr(OpenSearchLexicalIndex, "_request", corruptSecondItem)
        units = tuple(LexicalUnit(documentId="document-first", revisionId="revision-first",
            unitId=f"unit-{position}", sectionId="section-first", contentTokens="工程") for position in range(2))
        with pytest.raises(LexicalIndexError, match="acknowledge every unit"):
            native.writeBuild(buildId="partial-build", units=units)
        result = httpx.post(f"{endpoint}/{native.indexName}/_count", timeout=15, trust_env=False)
        result.raise_for_status()
        assert result.json()["count"] == 1
        assert native.deleteBuild("partial-build")
    finally:
        assert httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False).status_code in (200, 404)
        native.close()


@dataclass
class _BulkCapture:
    native: OpenSearchLexicalIndex
    requests: list[bytes]
    records: dict[str, dict[str, object]]


@pytest.fixture
def captureBulkRequests() -> Iterator[Callable[[int | None, int | None], _BulkCapture]]:
    clients: list[httpx.Client] = []
    handles: list[OpenSearchLexicalIndex] = []

    def createCapture(maximumBulkBytes: int | None = None, failingRequest: int | None = None) -> _BulkCapture:
        requests: list[bytes] = []
        records: dict[str, dict[str, object]] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/_count"):
                return httpx.Response(200, json={"count": len(records), "_shards": {"failed": 0}})
            assert request.url.path.endswith("/_bulk")
            assert request.url.params["refresh"] == "wait_for"
            requests.append(request.content)
            if len(requests) == failingRequest:
                return httpx.Response(503, json={"error": {"type": "unavailable"}})
            lines: list[str] = request.content.decode("utf-8").splitlines()
            assert request.content.endswith(b"\n") and len(lines) % 2 == 0
            items: list[dict[str, dict[str, int]]] = []
            for position in range(0, len(lines), 2):
                action: dict[str, dict[str, str]] = json.loads(lines[position])
                records[action["index"]["_id"]] = json.loads(lines[position + 1])
                items.append({"index": {"status": 201}})
            return httpx.Response(200, json={"errors": False, "items": items})

        endpoint: str = f"http://127.0.0.1:{10000 + int(uuid4().hex[:4], 16) % 50000}"
        scope: LexicalScope = LexicalScope(ownerId="bulk-contract", namespace="byte-packing")
        native: OpenSearchLexicalIndex = (
            OpenSearchLexicalIndex(endpoint=endpoint, scope=scope)
            if maximumBulkBytes is None else
            OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, maximumBulkBytes=maximumBulkBytes)
        )
        client: httpx.Client = httpx.Client(base_url=endpoint, transport=httpx.MockTransport(respond))
        native._client = client
        clients.append(client)
        handles.append(native)
        return _BulkCapture(native=native, requests=requests, records=records)

    try:
        yield createCapture
    finally:
        for handle in handles:
            handle.close()
        for client in clients:
            client.close()


def _createBulkUnits(count: int, *, content: str = "工程") -> tuple[LexicalUnit, ...]:
    return tuple(LexicalUnit(documentId="document", revisionId="revision", sectionId="section",
        unitId=str(position), contentTokens=content) for position in range(count))


def test_bulk_revision_that_fits_uses_one_request_above_two_hundred_units(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    capture: _BulkCapture = captureBulkRequests(None, None)
    capture.native.writeBuild(buildId="build", units=_createBulkUnits(201))
    assert len(capture.requests) == 1
    assert len(capture.records) == 201


def test_bulk_packing_counts_utf8_bytes_and_preserves_complete_units(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    budget: int = 800
    content: str = "验收计划" * 30
    capture: _BulkCapture = captureBulkRequests(budget, None)
    capture.native.writeBuild(buildId="build", units=_createBulkUnits(3, content=content))
    assert len(capture.requests) == 3
    assert all(len(body) <= budget for body in capture.requests)
    assert [row["contentTokens"] for row in capture.records.values()] == [content] * 3


def test_bulk_packing_accepts_the_exact_encoded_request_boundary(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    probe: _BulkCapture = captureBulkRequests(None, None)
    probe.native.writeBuild(buildId="build", units=_createBulkUnits(1))
    budget: int = len(probe.requests[0])
    capture: _BulkCapture = captureBulkRequests(budget, None)
    capture.native.writeBuild(buildId="build", units=_createBulkUnits(2))
    assert len(capture.requests) == 2
    assert all(len(body) == budget for body in capture.requests)
    assert len(capture.records) == 2


def test_oversized_unit_fails_without_sending_an_over_limit_request(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    capture: _BulkCapture = captureBulkRequests(800, None)
    with pytest.raises(LexicalIndexError, match="byte limit"):
        capture.native.writeBuild(buildId="build", units=_createBulkUnits(1, content="验收" * 1000))
    assert not capture.requests
    assert not capture.records


def test_later_bulk_failure_stops_before_another_request(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    probe: _BulkCapture = captureBulkRequests(None, None)
    probe.native.writeBuild(buildId="build", units=_createBulkUnits(1))
    capture: _BulkCapture = captureBulkRequests(len(probe.requests[0]), 2)
    with pytest.raises(LexicalIndexError, match="HTTP 503"):
        capture.native.writeBuild(buildId="build", units=_createBulkUnits(3))
    assert len(capture.requests) == 2
    assert len(capture.records) == 1


def test_native_rpc_uses_the_remaining_execution_budget() -> None:
    from shared.services.retrieval.lexical.deadline import IndexingDeadline

    timeouts: list[dict[str, float | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"cluster_uuid": "deadline-cluster"})

    native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
        endpoint=f"http://127.0.0.1:{10000 + int(uuid4().hex[:4], 16) % 50000}",
        scope=LexicalScope(ownerId="deadline", namespace="deadline"),
        deadline=IndexingDeadline(0.2), timeoutSeconds=30)
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="http://deadline.invalid") as client:
        native._client = client
        try:
            assert native.getClusterUuid() == "deadline-cluster"
            assert len(timeouts) == 1
            assert all(value is not None and 0 < value <= 0.2 for value in timeouts[0].values())
        finally:
            native.close()


def test_failed_bulk_item_is_rejected_even_if_response_errors_flag_is_false(
    captureBulkRequests: Callable[[int | None, int | None], _BulkCapture],
) -> None:
    capture: _BulkCapture = captureBulkRequests(None, None)
    original: Callable[..., object] = capture.native._request

    def corruptAcknowledgment(method: str, path: str, **options: object) -> object:
        body: object = original(method, path, **options)
        if "/_bulk?" in path:
            return {"errors": False, "items": [{"index": {"status": 500}}]}
        return body

    capture.native._request = corruptAcknowledgment
    with pytest.raises(LexicalIndexError, match="acknowledge every unit"):
        capture.native.writeBuild(buildId="build", units=_createBulkUnits(1))


@pytest.mark.parametrize("status,isUnavailable", [(401, True), (403, True), (404, True), (409, True),
    (429, True), (503, True), (400, False), (413, False), (422, False)])
def test_native_service_failures_are_distinct_from_corrupt_input(status: int, isUnavailable: bool) -> None:
    response: httpx.Response = httpx.Response(status, json={"error": {"type": "injected_failure"}})
    assert LexicalIndexError.fromHttpResponse(response).isRetryable is isUnavailable
    with pytest.raises(LexicalIndexError) as failed:
        OpenSearchLexicalIndex.validateBulkResponse({"errors": True,
            "items": [{"index": {"status": status, "error": {"type": "injected_failure"}}}]}, expectedUnitCount=1)
    assert failed.value.isRetryable is isUnavailable
