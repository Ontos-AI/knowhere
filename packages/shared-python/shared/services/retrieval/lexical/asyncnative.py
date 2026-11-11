"""Await native I/O while sharing the worker's query, mapping and validation rules."""

import asyncio
from collections.abc import Awaitable, Callable
import re
import ssl

import httpx
from pydantic import ValidationError

from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
from shared.services.retrieval.lexical.bulk import LexicalBulk
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
from shared.services.retrieval.lexical.opensearch import (
    OpenSearchLexicalIndex, _Acknowledgment, _CloseAcknowledgment, _SettingsResponse, _CountResponse,
)
from shared.services.retrieval.lexical.scope import LexicalScope
from shared.services.retrieval.lexical.searchplan import NativeSearchPlan
from shared.services.retrieval.lexical.unit import LexicalUnit
from shared.services.retrieval.lexical.stage_observer import StageObserver


class AsyncOpenSearchLexicalIndex:
    def __init__(self, *, endpoint: str, scope: LexicalScope,
                 indexPrefix: str = "knowhere-lexical-v1", timeoutSeconds: float = 30,
                 maximumBulkBytes: int = 4 * 1024 * 1024,
                 deadline: IndexingDeadline | None = None, preferContentCoverage: bool = False) -> None:
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
        self._preferContentCoverage: bool = preferContentCoverage
        self._client: httpx.AsyncClient = AsyncOpenSearchRuntime.resolve(endpoint).client
        self._isClosed: bool = False

    def forScope(self, scope: LexicalScope) -> "AsyncOpenSearchLexicalIndex":
        return AsyncOpenSearchLexicalIndex(endpoint=self._endpoint, scope=scope,
            indexPrefix=self._indexPrefix, timeoutSeconds=self._timeoutSeconds,
            maximumBulkBytes=self._maximumBulkBytes, deadline=self._deadline,
            preferContentCoverage=self._preferContentCoverage)

    async def getLifecycleSnapshot(self, *, isExact: bool,
                                   timeoutSeconds: float) -> NativeLifecycleSnapshot:
        try:
            return NativeLifecycleSnapshot.model_validate(await self._request("GET",
                OpenSearchLexicalIndex.buildLifecyclePath(self.indexName, isExact=isExact),
                timeoutSeconds=timeoutSeconds))
        except ValidationError as error:
            raise LexicalIndexError("Invalid native lifecycle state", isRetryable=True) from error

    async def setIndexOpen(self, *, isOpen: bool, operationId: str, timeoutSeconds: float) -> None:
        action: str = "_open" if isOpen else "_close"
        try:
            body: object = await self._request("POST",
                OpenSearchLexicalIndex._lifecyclePath(f"/{self.indexName}/{action}", timeoutSeconds),
                operationId=operationId, timeoutSeconds=timeoutSeconds)
            if isOpen:
                response: _Acknowledgment = _Acknowledgment.model_validate(body)
                isComplete: bool = response.acknowledged and response.shards_acknowledged
            else:
                closed: _CloseAcknowledgment = _CloseAcknowledgment.model_validate(body)
                isComplete = (closed.acknowledged and closed.shards_acknowledged
                    and set(closed.indices) == {self.indexName} and closed.indices[self.indexName].closed)
        except ValidationError as error:
            raise LexicalIndexError("Invalid native lifecycle acknowledgment", isRetryable=True,
                hasUnknownOutcome=True) from error
        if not isComplete:
            raise LexicalIndexError("Native lifecycle operation did not complete", isRetryable=True,
                hasUnknownOutcome=True)

    async def createIndex(self, *, operationId: str | None = None,
                          timeoutSeconds: float = 30) -> None:
        response: _Acknowledgment = _Acknowledgment.model_validate(await self._request("PUT",
            OpenSearchLexicalIndex._lifecyclePath(f"/{self.indexName}", timeoutSeconds),
            payload=OpenSearchLexicalIndex.buildIndexPayload(), operationId=operationId,
            timeoutSeconds=timeoutSeconds))
        if not response.acknowledged or not response.shards_acknowledged:
            raise LexicalIndexError("Native index creation has an uncertain outcome", isRetryable=True)

    async def getIndexUuid(self) -> str:
        response: _SettingsResponse = _SettingsResponse.model_validate(
            await self._request("GET", f"/{self.indexName}/_settings?flat_settings=true"))
        metadata = response.root.get(self.indexName)
        identity: object = metadata.settings.get("index.uuid") if metadata else None
        if not isinstance(identity, str) or not identity:
            raise LexicalIndexError("Native index identity is missing")
        return identity

    async def search(self, *, buildIds: tuple[str, ...], queryTokens: tuple[str, ...],
                     limit: int, unitIds: tuple[str, ...] | None = None,
                     sectionIds: tuple[str, ...] | None = None,
                     buildLookup: dict[str, str] | None = None,
                     extraFilters: tuple[dict[str, object], ...] = (),
                     exclusions: tuple[dict[str, object], ...] = ()) -> tuple[LexicalHit, ...]:
        if (not buildIds and buildLookup is None) or not queryTokens or limit <= 0 or unitIds == () or sectionIds == ():
            return ()

        async def queryChannel(channel: str) -> object:
            StageObserver.add("path_channel_calls" if channel == "pathTokens" else "content_channel_calls")
            return await self._request("POST",
                f"/{self.indexName}/_search?allow_partial_search_results=false&ignore_unavailable=false",
                payload=NativeSearchPlan.buildPayload(channel, buildIds=buildIds,
                    queryTokens=queryTokens, limit=limit, unitIds=unitIds,
                    preferContentCoverage=self._preferContentCoverage, sectionIds=sectionIds,
                    buildLookup=buildLookup, extraFilters=extraFilters, exclusions=exclusions))

        tasks: tuple[asyncio.Task[object], asyncio.Task[object]] = (
            asyncio.create_task(queryChannel("pathTokens")),
            asyncio.create_task(queryChannel("contentTokens")),
        )
        try:
            bodies: list[object] = list(await asyncio.gather(*tasks, return_exceptions=True))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for body in bodies:
            if isinstance(body, asyncio.CancelledError):
                raise LexicalIndexError("Native channel cancellation has an unknown outcome",
                    isRetryable=True, hasUnknownOutcome=True) from body
            if isinstance(body, LexicalIndexError) and body.hasUnknownOutcome:
                raise body
        for body in bodies:
            if isinstance(body, BaseException):
                raise body
        return NativeSearchPlan.fuseChannels((bodies[0], bodies[1]),
            buildIds=buildIds if buildLookup is None else None, limit=limit)

    async def writePublishedManifest(self, *, manifestId: str, scopeId: str, buildIds: tuple[str, ...]) -> None:
        response: object = await self._request("PUT", f"/{self._indexPrefix}-published-manifests/_create/{manifestId}",
            payload={"scope": scopeId, "buildIds": buildIds})
        if not isinstance(response, dict) or response.get("_id") != manifestId or response.get("result") != "created":
            raise LexicalIndexError("Native manifest creation did not confirm the candidate", isRetryable=True)

    async def writeBuild(self, *, buildId: str, units: tuple[LexicalUnit, ...],
                         beforeBatch: Callable[[], Awaitable[None]] | None = None) -> None:
        for batch in LexicalBulk.createRequests(buildId=buildId, units=units, maximumBytes=self._maximumBulkBytes):
            if beforeBatch is not None:
                await beforeBatch()
            body: object = await self._request("POST", f"/{self.indexName}/_bulk?refresh=wait_for", textBody=batch.textBody)
            OpenSearchLexicalIndex.validateBulkResponse(body, expectedUnitCount=batch.unitCount)
        try:
            visible: _CountResponse = _CountResponse.model_validate(await self._request("POST",
                f"/{self.indexName}/_count", payload={"query": {"term": {"buildId": buildId}}}))
        except ValidationError as error:
            raise LexicalIndexError("Invalid native visibility response") from error
        if visible.shards.failed or visible.count != len(units):
            raise LexicalIndexError("Lexical visible build count does not match its input")

    def close(self) -> None:
        # API lifespan owns the loop-scoped client, not an individual index handle.
        self._isClosed = True

    async def _request(self, method: str, path: str, *, payload: object | None = None,
                       textBody: str | None = None, operationId: str | None = None,
                       timeoutSeconds: float | None = None) -> object:
        if self._isClosed:
            raise LexicalIndexError("Native lexical handle is closed")
        headers: dict[str, str] = {}
        if textBody is not None:
            headers["Content-Type"] = "application/x-ndjson"
        if operationId is not None:
            headers["X-Opaque-Id"] = operationId
        try:
            requestTimeout: float = timeoutSeconds if timeoutSeconds is not None else self._timeoutSeconds
            if self._deadline is not None:
                requestTimeout = min(requestTimeout, self._deadline.getRemainingSeconds())
            observationStart: float | None = StageObserver.startNativeRequest(path, textBody)
            with StageObserver.bindNativeRequest():
                response: httpx.Response = await self._client.request(method, path,
                    json=payload if textBody is None else None, content=textBody, headers=headers,
                    timeout=requestTimeout)
            StageObserver.finishNativeResponse(response, path, observationStart)
            response.raise_for_status()
            if self._deadline is not None:
                self._deadline.getRemainingSeconds()
            with StageObserver.measure("native_response_processing_ms"):
                return response.json()
        except httpx.HTTPStatusError as error:
            raise LexicalIndexError.fromHttpResponse(error.response) from error
        except (httpx.TransportError, ssl.SSLError) as error:
            raise LexicalIndexError("Native lexical transport failed", isRetryable=True,
                hasUnknownOutcome=True) from error
        except (ValueError, ValidationError) as error:
            raise LexicalIndexError("Native lexical response was invalid") from error
