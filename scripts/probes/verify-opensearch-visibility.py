"""Verify real OpenSearch visibility with synthetic data on a local test node.

This probe does not implement or accept Knowhere's publication integration.
Use --skip-visibility-barrier as an intentionally failing negative control.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

JsonObject = dict[str, object]
EXPECTED_VERSION: str = "3.9.0"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def requireObject(value: object) -> JsonObject:
    require(isinstance(value, dict), "Expected a JSON object")
    return cast(JsonObject, value)


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: JsonObject
    elapsedMilliseconds: float


class OpenSearchVisibilityProbe:
    def __init__(self, endpoint: str) -> None:
        parsedEndpoint = urlparse(endpoint)
        require(
            parsedEndpoint.hostname in {"localhost", "127.0.0.1"},
            "Use an isolated local node",
        )
        self.endpoint: str = endpoint.rstrip("/")
        self.prefix: str = f"knowhere-visibility-{uuid4().hex[:12]}"
        self.indexNames: list[str] = []

    def requestObject(
        self,
        method: str,
        path: str,
        payload: JsonObject | str | None = None,
    ) -> HttpResult:
        encodedPayload: bytes | None = None
        contentType: str = "application/json"
        if isinstance(payload, str):
            encodedPayload = payload.encode("utf-8")
            contentType = "application/x-ndjson"
        elif payload is not None:
            encodedPayload = json.dumps(payload).encode("utf-8")
        request: Request = Request(
            self.endpoint + path,
            data=encodedPayload,
            method=method,
            headers={"Content-Type": contentType},
        )
        startedAt: float = time.monotonic()
        status: int
        responseBytes: bytes
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                responseBytes = response.read()
        except HTTPError as err:
            status = err.code
            responseBytes = err.read()
        decodedPayload: object = json.loads(responseBytes)
        return HttpResult(
            status,
            requireObject(decodedPayload),
            round((time.monotonic() - startedAt) * 1_000, 3),
        )

    def requestSuccess(
        self,
        method: str,
        path: str,
        payload: JsonObject | str | None = None,
    ) -> HttpResult:
        result: HttpResult = self.requestObject(method, path, payload)
        require(200 <= result.status < 300, f"{method} {path}: HTTP {result.status}")
        return result

    def createIndex(self, suffix: str, interval: str = "1s") -> str:
        indexName: str = f"{self.prefix}-{suffix}"
        self.indexNames.append(indexName)
        self.requestSuccess(
            "PUT",
            f"/{indexName}",
            {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "refresh_interval": interval,
                    "search.idle.after": "1s",
                    "similarity": {
                        "knowhere_bm25": {"type": "BM25", "k1": 1.5, "b": 0.75}
                    },
                },
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "path": {
                            "type": "text",
                            "analyzer": "whitespace",
                            "similarity": "knowhere_bm25",
                        },
                        "content": {
                            "type": "text",
                            "analyzer": "whitespace",
                            "similarity": "knowhere_bm25",
                        },
                        "revision": {"type": "keyword"},
                        "ordinal": {"type": "integer"},
                    },
                },
            },
        )
        return indexName

    def buildBulk(self, count: int, revision: str = "revision-one") -> str:
        lines: list[str] = []
        for index in range(count):
            lines.append(json.dumps({"index": {"_id": f"unit-{index}"}}))
            lines.append(
                json.dumps(
                    {
                        "path": f"manual section{index} pathmarker",
                        "content": f"contentmarker payload payload unit{index}",
                        "revision": revision,
                        "ordinal": index,
                    }
                )
            )
        return "\n".join(lines) + "\n"

    def requireBulkSuccess(self, result: HttpResult, expectedCount: int) -> None:
        require(result.status == 200, f"Bulk HTTP status: {result.status}")
        require(result.payload.get("errors") is False, "Bulk contains item failures")
        items: list[object] = cast(list[object], result.payload.get("items", []))
        require(
            len(items) == expectedCount, "Bulk item count differs from revision size"
        )
        for item in items:
            operation: JsonObject = requireObject(requireObject(item)["index"])
            status: int = cast(int, operation.get("status", 0))
            require(200 <= status < 300, "A bulk item did not succeed")

    def searchCount(self, indexName: str, query: JsonObject | None = None) -> int:
        result: HttpResult = self.requestSuccess(
            "POST",
            f"/{indexName}/_search",
            {
                "query": query or {"match_all": {}},
                "size": 0,
                "track_total_hits": True,
            },
        )
        shards: JsonObject = requireObject(result.payload["_shards"])
        require(shards.get("failed") == 0, "Search returned shard failures")
        hits: JsonObject = requireObject(result.payload["hits"])
        total: JsonObject = requireObject(hits["total"])
        return cast(int, total["value"])

    def verifyUnsafeCompletion(self) -> JsonObject:
        indexName: str = self.createIndex("negative-control", "-1")
        result: HttpResult = self.requestSuccess(
            "POST",
            f"/{indexName}/_bulk?refresh=false",
            self.buildBulk(188),
        )
        self.requireBulkSuccess(result, 188)
        visibleCount: int = self.searchCount(indexName)
        require(
            visibleCount == 188,
            f"Premature completion: acknowledged 188 writes but visible={visibleCount}",
        )
        return {"visible": visibleCount}

    def verifyPendingBarrier(self) -> JsonObject:
        indexName: str = self.createIndex("pending-barrier", "-1")
        visibleBefore: int = -1
        hasReturnedBeforeRefresh: bool = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            pendingWrite: Future[HttpResult] = executor.submit(
                self.requestSuccess,
                "POST",
                f"/{indexName}/_bulk?refresh=wait_for",
                self.buildBulk(188),
            )
            try:
                deadline: float = time.monotonic() + 10
                while True:
                    document: HttpResult = self.requestObject(
                        "GET", f"/{indexName}/_doc/unit-0"
                    )
                    if document.payload.get("found") is True:
                        break
                    require(time.monotonic() < deadline, "Write did not reach the node")
                    time.sleep(0.05)
                visibleBefore = self.searchCount(indexName)
                hasReturnedBeforeRefresh = pendingWrite.done()
                require(
                    visibleBefore == 0, "Controlled unrefreshed data became visible"
                )
                require(
                    not hasReturnedBeforeRefresh,
                    "wait_for acknowledged before visibility",
                )
            finally:
                refresh: HttpResult = self.requestSuccess(
                    "POST", f"/{indexName}/_refresh"
                )
                require(
                    requireObject(refresh.payload["_shards"]).get("failed") == 0,
                    "Refresh failed",
                )
            result: HttpResult = pendingWrite.result(timeout=30)
        self.requireBulkSuccess(result, 188)
        visibleAfter: int = self.searchCount(indexName)
        require(
            visibleAfter == 188, "Search immediately after confirmation missed units"
        )
        return {
            "visibleBeforeRefresh": visibleBefore,
            "returnedBeforeRefresh": hasReturnedBeforeRefresh,
            "visibleImmediatelyAfterConfirmation": visibleAfter,
        }

    def verifyScheduledVisibility(self) -> JsonObject:
        indexName: str = self.createIndex("scheduled")
        rounds: list[JsonObject] = []
        for index in range(3):
            if index > 0:
                time.sleep(1.2)
            revision: str = f"revision-{index}"
            result: HttpResult = self.requestSuccess(
                "POST",
                f"/{indexName}/_bulk?refresh=wait_for",
                self.buildBulk(352, revision),
            )
            self.requireBulkSuccess(result, 352)
            revisionQuery: JsonObject = {"term": {"revision": revision}}
            require(
                self.searchCount(indexName, revisionQuery) == 352,
                "Latest revision is incomplete",
            )
            require(
                self.searchCount(indexName, {"match": {"path": "pathmarker"}}) == 352,
                "Path visibility failed",
            )
            require(
                self.searchCount(indexName, {"match": {"content": "contentmarker"}})
                == 352,
                "Content visibility failed",
            )
            require(self.searchCount(indexName) == 352, "Replay duplicated units")
            rounds.append(
                {
                    "round": index + 1,
                    "visibleUnits": 352,
                    "bulkAndRefreshMilliseconds": result.elapsedMilliseconds,
                }
            )
        return {
            "rounds": rounds,
            "refreshInterval": "explicit 1s",
            "idleThreshold": "1s",
            "searchRetries": 0,
            "manualRefreshes": 0,
        }

    def verifyPartialFailure(self) -> JsonObject:
        indexName: str = self.createIndex("partial")
        lines: list[str] = [
            json.dumps({"index": {"_id": "valid"}}),
            json.dumps(
                {"path": "pathmarker", "content": "contentmarker", "ordinal": 1}
            ),
            json.dumps({"index": {"_id": "invalid"}}),
            json.dumps(
                {
                    "path": "pathmarker",
                    "content": "contentmarker",
                    "ordinal": "invalid-integer",
                }
            ),
        ]
        result: HttpResult = self.requestSuccess(
            "POST", f"/{indexName}/_bulk?refresh=wait_for", "\n".join(lines) + "\n"
        )
        require(
            result.payload.get("errors") is True, "Invalid item unexpectedly succeeded"
        )
        hasRejectedIncompleteRevision: bool = False
        try:
            self.requireBulkSuccess(result, 2)
        except AssertionError:
            hasRejectedIncompleteRevision = True
        require(
            hasRejectedIncompleteRevision,
            "Completion guard accepted a partial revision",
        )
        require(
            self.searchCount(indexName) == 1,
            "Expected exactly one successfully indexed unit",
        )
        return {
            "httpStatus": result.status,
            "hasItemErrors": True,
            "visibleUnits": 1,
            "hasRejectedIncompleteRevision": hasRejectedIncompleteRevision,
        }

    def verifyCloseReopen(self) -> JsonObject:
        indexName: str = self.createIndex("reopen")
        self.requireBulkSuccess(
            self.requestSuccess(
                "POST",
                f"/{indexName}/_bulk?refresh=wait_for",
                self.buildBulk(188),
            ),
            188,
        )
        self.requestSuccess("POST", f"/{indexName}/_close")
        closedSearch: HttpResult = self.requestObject(
            "POST", f"/{indexName}/_search", {"query": {"match_all": {}}}
        )
        require(closedSearch.status == 400, "Closed index did not reject search")
        self.requestSuccess("POST", f"/{indexName}/_open?wait_for_active_shards=1")
        require(
            self.searchCount(indexName) == 188, "Reopened index lost searchable units"
        )
        self.requireBulkSuccess(
            self.requestSuccess(
                "POST",
                f"/{indexName}/_bulk?refresh=wait_for",
                self.buildBulk(188, "revision-two"),
            ),
            188,
        )
        require(
            self.searchCount(indexName, {"term": {"revision": "revision-two"}}) == 188,
            "Write after reopen incomplete",
        )
        require(
            self.searchCount(indexName, {"term": {"revision": "revision-one"}}) == 0,
            "Overwritten units remained current",
        )
        return {
            "closedSearchHttpStatus": closedSearch.status,
            "unitsAfterReopen": 188,
            "unitsAfterRevisionReplacement": 188,
        }

    def verifyTokenizerAndMapping(self) -> JsonObject:
        indexName: str = self.createIndex("tokens")
        analysis: HttpResult = self.requestSuccess(
            "POST",
            f"/{indexName}/_analyze",
            {
                "field": "content",
                "text": "知识 检索 检索 BM25",
            },
        )
        tokens: list[object] = cast(list[object], analysis.payload["tokens"])
        actualTokens: list[object] = [requireObject(token)["token"] for token in tokens]
        require(
            actualTokens == ["知识", "检索", "检索", "BM25"],
            "Whitespace altered pre-tokenized words or frequencies",
        )
        mapping: JsonObject = self.requestSuccess(
            "GET", f"/{indexName}/_mapping"
        ).payload
        properties: JsonObject = requireObject(
            requireObject(requireObject(mapping[indexName])["mappings"])["properties"]
        )
        for fieldName in ("path", "content"):
            field: JsonObject = requireObject(properties[fieldName])
            require(field.get("analyzer") == "whitespace", "Unexpected analyzer")
            require(
                field.get("similarity") == "knowhere_bm25",
                "Similarity is not bound to field",
            )
        settings: JsonObject = self.requestSuccess(
            "GET", f"/{indexName}/_settings"
        ).payload
        indexSettings: JsonObject = requireObject(
            requireObject(requireObject(settings[indexName])["settings"])["index"]
        )
        similarity: JsonObject = requireObject(
            requireObject(indexSettings["similarity"])["knowhere_bm25"]
        )
        require(float(cast(str, similarity["k1"])) == 1.5, "Unexpected k1")
        require(float(cast(str, similarity["b"])) == 0.75, "Unexpected b")
        return {
            "hasPreservedRepeatedTokens": True,
            "analyzer": "whitespace",
            "k1": 1.5,
            "b": 0.75,
        }

    def verifyNamespaceIsolation(self) -> JsonObject:
        firstIndex: str = self.createIndex("namespace-one")
        secondIndex: str = self.createIndex("namespace-two")
        for indexName, count in ((firstIndex, 1), (secondIndex, 2)):
            self.requireBulkSuccess(
                self.requestSuccess(
                    "POST",
                    f"/{indexName}/_bulk?refresh=wait_for",
                    self.buildBulk(count),
                ),
                count,
            )
        require(
            self.searchCount(firstIndex) == 1,
            "First namespace included other namespace units",
        )
        require(
            self.searchCount(secondIndex) == 2, "Second namespace count was incorrect"
        )
        return {
            "firstNamespaceUnits": 1,
            "secondNamespaceUnits": 2,
            "primaryShardsPerIndex": 1,
        }

    def cleanIndexes(self) -> None:
        for indexName in reversed(self.indexNames):
            require(
                indexName.startswith(self.prefix + "-"), "Refused unowned index cleanup"
            )
            self.requestSuccess("DELETE", f"/{indexName}")


def runProbe() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:19289")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--skip-visibility-barrier", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    probe: OpenSearchVisibilityProbe = OpenSearchVisibilityProbe(arguments.endpoint)
    report: JsonObject = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "scope": "Synthetic OpenSearch protocol verification; not Knowhere end-to-end acceptance",
        "hasSkippedBarrier": arguments.skip_visibility_barrier,
    }
    observations: list[JsonObject] = []
    exitCode: int = 0
    try:
        root: JsonObject = probe.requestSuccess("GET", "/").payload
        version: JsonObject = requireObject(root["version"])
        require(
            version.get("number") == EXPECTED_VERSION,
            "Node version differs from pinned probe version",
        )
        report["opensearchVersion"] = version["number"]
        cases: list[Callable[[], JsonObject]] = (
            [probe.verifyUnsafeCompletion]
            if arguments.skip_visibility_barrier
            else [
                probe.verifyPendingBarrier,
                probe.verifyScheduledVisibility,
                probe.verifyPartialFailure,
                probe.verifyCloseReopen,
                probe.verifyTokenizerAndMapping,
                probe.verifyNamespaceIsolation,
            ]
        )
        for verifyCase in cases:
            result: JsonObject = {
                "case": verifyCase.__name__,
                "status": "passed",
                "observations": verifyCase(),
            }
            observations.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
        report["status"] = "passed"
    except Exception as err:
        report["status"] = "failed"
        report["error"] = f"{type(err).__name__}: {err}"
        print(json.dumps({"status": "failed", "error": report["error"]}), flush=True)
        exitCode = 1
    finally:
        try:
            probe.cleanIndexes()
            report["hasCleanedIndexes"] = True
        except Exception as err:
            report["hasCleanedIndexes"] = False
            report["cleanupError"] = f"{type(err).__name__}: {err}"
            report["status"] = "failed"
            exitCode = 1
        report["cases"] = observations
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, indent=2) + "\n")
    return exitCode


if __name__ == "__main__":
    raise SystemExit(runProbe())
