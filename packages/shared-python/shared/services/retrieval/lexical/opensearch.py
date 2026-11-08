"""Native BM25 storage; callers own admission, use protection, and publication."""

from __future__ import annotations

from collections.abc import Callable
import re
import ssl

import httpx
from pydantic import BaseModel, Field, RootModel, ValidationError

from shared.services.retrieval.lexical.bulk import LexicalBulk
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
from shared.services.retrieval.lexical.searchplan import NativeSearchPlan, _Shards
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.lexical.stage_observer import StageObserver


class _BulkItem(BaseModel):
    status: int
    error: dict[str, object] | None = None


class _BulkResponse(BaseModel):
    errors: bool
    items: tuple[dict[str, _BulkItem], ...]


class _CountResponse(BaseModel):
    count: int
    shards: _Shards = Field(alias="_shards")


class _DeleteResponse(BaseModel):
    timed_out: bool
    version_conflicts: int
    failures: tuple[object, ...]


class _Acknowledgment(BaseModel):
    acknowledged: bool
    shards_acknowledged: bool


class _ClosedIndex(BaseModel):
    closed: bool


class _CloseAcknowledgment(_Acknowledgment):
    indices: dict[str, _ClosedIndex]


class _IndexSettings(BaseModel):
    settings: dict[str, object]


class _SettingsResponse(RootModel[dict[str, _IndexSettings]]):
    pass


class _ClusterIdentity(BaseModel):
    cluster_uuid: str


class OpenSearchLexicalIndex:
    def __init__(
        self,
        *,
        endpoint: str,
        scope: LexicalScope,
        indexPrefix: str = "knowhere-lexical-v1",
        timeoutSeconds: float = 30,
        maximumBulkBytes: int = 4 * 1024 * 1024,
        deadline: IndexingDeadline | None = None,
    ) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", indexPrefix) is None:
            raise ValueError("Lexical index prefix must be a lowercase identifier")
        if maximumBulkBytes <= 0:
            raise ValueError("Lexical bulk byte limit must be positive")
        self.indexName: str = f"{indexPrefix}-{scope.fingerprint[:48]}"
        self._endpoint: str = endpoint
        self._indexPrefix: str = indexPrefix
        self._timeoutSeconds: float = timeoutSeconds
        self._maximumBulkBytes: int = maximumBulkBytes
        self._deadline: IndexingDeadline | None = deadline
        self._runtime: OpenSearchRuntime = OpenSearchRuntime.resolve(endpoint)
        self._client: httpx.Client = self._runtime.client
        self._isClosed: bool = False

    def forScope(self, scope: LexicalScope) -> "OpenSearchLexicalIndex":
        return OpenSearchLexicalIndex(
            endpoint=self._endpoint, scope=scope, indexPrefix=self._indexPrefix,
            timeoutSeconds=self._timeoutSeconds, maximumBulkBytes=self._maximumBulkBytes,
            deadline=self._deadline,
        )

    def getLifecycleSnapshot(
        self, *, isExact: bool, timeoutSeconds: float
    ) -> NativeLifecycleSnapshot:
        try:
            return NativeLifecycleSnapshot.model_validate(
                self._request(
                    "GET",
                    self.buildLifecyclePath(self.indexName, isExact=isExact),
                    timeoutSeconds=timeoutSeconds,
                )
            )
        except ValidationError as error:
            raise LexicalIndexError(
                "Invalid native lifecycle state", isRetryable=True
            ) from error

    @staticmethod
    def buildLifecyclePath(indexName: str, *, isExact: bool) -> str:
        metrics: str = "nodes,metadata,routing_table,blocks"
        target: str = f"/{indexName}" if isExact else ""
        fields: str = (
            "cluster_uuid,nodes.*.ephemeral_id,metadata.indices.*.state,"
            "metadata.indices.*.settings.index.uuid,routing_table.indices.*.shards.*.primary,"
            "routing_table.indices.*.shards.*.state,blocks.global.*.levels,blocks.indices.*.*.levels"
        )
        return f"/_cluster/state/{metrics}{target}?ignore_unavailable=true&expand_wildcards=all&filter_path={fields}"

    def setIndexOpen(
        self, *, isOpen: bool, operationId: str, timeoutSeconds: float
    ) -> None:
        action: str = "_open" if isOpen else "_close"
        try:
            body: object = self._request(
                "POST",
                self._lifecyclePath(f"/{self.indexName}/{action}", timeoutSeconds),
                operationId=operationId,
                timeoutSeconds=timeoutSeconds,
            )
            if isOpen:
                response: _Acknowledgment = _Acknowledgment.model_validate(body)
                isComplete: bool = (
                    response.acknowledged and response.shards_acknowledged
                )
            else:
                closed: _CloseAcknowledgment = _CloseAcknowledgment.model_validate(body)
                isComplete = (
                    closed.acknowledged
                    and closed.shards_acknowledged
                    and set(closed.indices) == {self.indexName}
                    and closed.indices[self.indexName].closed
                )
        except ValidationError as error:
            raise LexicalIndexError(
                "Invalid native lifecycle acknowledgment",
                isRetryable=True,
                hasUnknownOutcome=True,
            ) from error
        if not isComplete:
            raise LexicalIndexError(
                "Native lifecycle operation did not complete",
                isRetryable=True,
                hasUnknownOutcome=True,
            )

    @staticmethod
    def _lifecyclePath(path: str, timeoutSeconds: float) -> str:
        timeout: str = f"{max(1, int(timeoutSeconds * 1000))}ms"
        return f"{path}?wait_for_active_shards=1&timeout={timeout}&cluster_manager_timeout={timeout}"

    def createIndex(
        self, *, operationId: str | None = None, timeoutSeconds: float = 30
    ) -> None:
        response: _Acknowledgment = _Acknowledgment.model_validate(
            self._request(
                "PUT",
                self._lifecyclePath(f"/{self.indexName}", timeoutSeconds),
                payload=self.buildIndexPayload(),
                operationId=operationId,
                timeoutSeconds=timeoutSeconds,
            )
        )
        if not response.acknowledged or not response.shards_acknowledged:
            raise LexicalIndexError(
                "Native index creation has an uncertain outcome", isRetryable=True
            )

    @staticmethod
    def buildIndexPayload() -> dict[str, object]:
        keywordFields: tuple[str, ...] = (
            "buildId",
            "documentId",
            "revisionId",
            "unitId",
            "sectionId",
            "sectionPath",
        )
        properties: dict[str, object] = {
            name: {"type": "keyword"} for name in keywordFields
        }
        properties.update({"sortOrder": {"type": "integer"}})
        properties.update(
            {name: {"type": "boolean"} for name in ("hasImage", "hasTable")}
        )
        properties.update(
            {
                name: {
                    "type": "text",
                    "analyzer": "whitespace",
                    "similarity": "knowhere",
                }
                for name in ("pathTokens", "contentTokens")
            }
        )
        return {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        "refresh_interval": "1s",
                        "similarity": {
                            "knowhere": {"type": "BM25", "k1": 1.5, "b": 0.75}
                        },
                    },
                    "mappings": {"dynamic": "strict", "properties": properties},
                }

    def getIndexUuid(self) -> str:
        response: _SettingsResponse = _SettingsResponse.model_validate(
            self._request("GET", f"/{self.indexName}/_settings?flat_settings=true")
        )
        metadata: _IndexSettings | None = response.root.get(self.indexName)
        identity: object = metadata.settings.get("index.uuid") if metadata else None
        if not isinstance(identity, str) or not identity:
            raise LexicalIndexError("Native index identity is missing")
        return identity

    def getClusterUuid(self) -> str:
        return _ClusterIdentity.model_validate(self._request("GET", "/")).cluster_uuid

    def writeBuild(
        self,
        *,
        buildId: str,
        units: tuple[LexicalUnit, ...],
        beforeBatch: Callable[[], None] | None = None,
    ) -> None:
        for batch in LexicalBulk.createRequests(
            buildId=buildId, units=units, maximumBytes=self._maximumBulkBytes
        ):
            if beforeBatch is not None:
                beforeBatch()
            self.validateBulkResponse(self._request(
                "POST", f"/{self.indexName}/_bulk?refresh=wait_for", textBody=batch.textBody,
            ), expectedUnitCount=batch.unitCount)
        visible: _CountResponse = _CountResponse.model_validate(
            self._request(
                "POST",
                f"/{self.indexName}/_count",
                payload={"query": {"term": {"buildId": buildId}}},
            )
        )
        if visible.shards.failed or visible.count != len(units):
            raise LexicalIndexError(
                "Lexical visible build count does not match its input"
            )

    @staticmethod
    def validateBulkResponse(body: object, *, expectedUnitCount: int) -> None:
        try:
            response: _BulkResponse = _BulkResponse.model_validate(body)
        except ValidationError as error:
            raise LexicalIndexError("Invalid native bulk acknowledgment") from error
        items: tuple[_BulkItem, ...] = tuple(item for operation in response.items for item in operation.values())
        hasValidItems: bool = all(set(operation) == {"index"} for operation in response.items)
        if (not response.errors and len(response.items) == expectedUnitCount and hasValidItems
            and all(item.status in (200, 201) and item.error is None for item in items)):
            return
        hasClosedItem: bool = any(item.error is not None and item.error.get("type") == "index_closed_exception" for item in items)
        raise LexicalIndexError("Lexical bulk did not acknowledge every unit",
            isRetryable=hasClosedItem or bool(items) and all(item.status < 300 or LexicalIndexError.isUnavailableStatus(item.status) for item in items),
            isIndexClosed=hasClosedItem)

    def search(
        self,
        *,
        buildIds: tuple[str, ...],
        queryTokens: tuple[str, ...],
        limit: int,
        unitIds: tuple[str, ...] | None = None,
        sectionIds: tuple[str, ...] | None = None,
    ) -> tuple[LexicalHit, ...]:
        if not buildIds or not queryTokens or limit <= 0 or unitIds == () or sectionIds == ():
            return ()
        def queryChannel(channel: str) -> object:
            StageObserver.add("path_channel_calls" if channel == "pathTokens" else "content_channel_calls")
            return self._request("POST",
                f"/{self.indexName}/_search?allow_partial_search_results=false&ignore_unavailable=false",
                payload=NativeSearchPlan.buildPayload(channel, buildIds=buildIds,
                    queryTokens=queryTokens, limit=limit, unitIds=unitIds, sectionIds=sectionIds))

        bodies: tuple[object, object] = self._runtime.queryChannels(queryChannel)
        return NativeSearchPlan.fuseChannels(bodies, buildIds=buildIds, limit=limit)

    def deleteBuild(self, buildId: str) -> bool:
        response: _DeleteResponse = _DeleteResponse.model_validate(
            self._request(
                "POST",
                f"/{self.indexName}/_delete_by_query?conflicts=proceed&refresh=true",
                payload={"query": {"term": {"buildId": buildId}}},
            )
        )
        if response.timed_out or response.failures or response.version_conflicts:
            return False
        remaining: _CountResponse = _CountResponse.model_validate(
            self._request(
                "POST",
                f"/{self.indexName}/_count",
                payload={"query": {"term": {"buildId": buildId}}},
            )
        )
        return not remaining.shards.failed and remaining.count == 0

    def close(self) -> None:
        # The process owns the transport; closing one scope must not interrupt
        # another request borrowing its pooled connections.
        self._isClosed = True

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        textBody: str | None = None,
        operationId: str | None = None,
        timeoutSeconds: float | None = None,
    ) -> object:
        if self._isClosed:
            raise LexicalIndexError("Native lexical handle is closed")
        try:
            headers: dict[str, str] = {}
            if textBody is not None:
                headers["Content-Type"] = "application/x-ndjson"
            if operationId is not None:
                headers["X-Opaque-Id"] = operationId
            requestTimeout: float = timeoutSeconds if timeoutSeconds is not None else self._timeoutSeconds
            if self._deadline is not None:
                requestTimeout = min(requestTimeout, self._deadline.getRemainingSeconds())
            observationStart: float | None = StageObserver.startNativeRequest(path, textBody)
            with StageObserver.bindNativeRequest():
                response: httpx.Response = self._client.request(
                    method, path, json=payload if textBody is None else None,
                    content=textBody, headers=headers, timeout=requestTimeout,
                )
            StageObserver.finishNativeResponse(response, path, observationStart)
            response.raise_for_status()
            if self._deadline is not None:
                self._deadline.getRemainingSeconds()
            with StageObserver.measure("native_response_processing_ms"):
                return response.json()
        except httpx.HTTPStatusError as err:
            raise LexicalIndexError.fromHttpResponse(err.response) from err
        except (httpx.TransportError, ssl.SSLError) as err:
            raise LexicalIndexError(
                "Native lexical transport failed",
                isRetryable=True,
                hasUnknownOutcome=True,
            ) from err
        except (ValueError, ValidationError) as err:
            raise LexicalIndexError("Native lexical response was invalid") from err
