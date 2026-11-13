"""Await one primary publication in its original HTTP request and async pool."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Literal

from loguru import logger

from shared.core.config import settings
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.asynccoordination import AsyncLexicalCoordination
from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.manifest_publication import ManifestPublication, ManifestPublicationAdvanced
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.preparation import LexicalPreparation
from shared.services.retrieval.lexical.settlement import IndexingSettlement
from shared.services.retrieval.lexical.storage import LexicalInputStorage
from shared.services.retrieval.lexical.worker import IndexingWorker, _Claim, _Context, _InputSource


class AsyncIndexingWorker:
    def __init__(self, database: AsyncLexicalDatabase, deadline: IndexingDeadline) -> None:
        self.database: AsyncLexicalDatabase = database
        self.deadline: IndexingDeadline = deadline
        self.repository: IndexingWorker = IndexingWorker(sessionFactory=database.sessions)
        self.repository._deadline = deadline
        self.errorCode: str | None = None
        self.hasPendingSettlement: bool = False

    async def settleExpired(self, jobId: str) -> None:
        def settle() -> str | None:
            with self.repository._transaction() as session:
                _, eventId = IndexingSettlement.settleExpired(session, jobId)
                return eventId
        async with asyncio.timeout(self.deadline.getRemainingSeconds()):
            eventId: str | None = await self.database.run(settle)
        SyncJobWebhookOutbox().enqueue_event_id_after_commit(eventId)

    async def run(self, jobId: str, *, attemptId: str) -> Literal["completed", "pending", "failed", "skipped"]:
        context: _Context | str | None = None
        claim: _Claim | None = None
        ownerToken: str | None = None
        native: AsyncOpenSearchLexicalIndex | None = None
        isKnown: bool = False
        try:
            async with asyncio.timeout(self.deadline.getRemainingSeconds()):
                context = await self.database.run(partial(self.repository._readContext, jobId))
                if isinstance(context, str):
                    return "completed" if context == "done" else "failed" if context == "failed" else "skipped"
                if context.attemptId != attemptId:
                    return "skipped"
                ownerToken = await self.database.run(partial(self.repository._startExecution, context))
                if ownerToken is None:
                    return "pending"
                if settings.KNOWHERE_LEXICAL_BACKEND == "postgresql":
                    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
                    publication = PostgresqlPublication(sessionFactory=self.repository.sessions, deadline=self.deadline)
                    completed = await publication.runAsync(context, ownerToken=ownerToken, database=self.database)
                    SyncJobWebhookOutbox().enqueue_event_id_after_commit(completed.eventId)
                    return "completed"
                native = AsyncOpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                    scope=context.scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
                    maximumBulkBytes=settings.KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES, deadline=self.deadline)
                coordination: AsyncLexicalCoordination = AsyncLexicalCoordination(self.database, executionDeadline=self.deadline)
                indexUuid, epoch = await coordination.ensureIndex(scope=context.scope, native=native)
                claim = await self.database.run(partial(self.repository._claim, context,
                    ownerToken=ownerToken, indexUuid=indexUuid, epoch=epoch))
                if claim is None:
                    raise LexicalIndexError("Indexing execution lost admission authority")
                payload: LexicalInput = await self._prepareInput(claim)
                await self.database.run(partial(self.repository._assertClaim, claim))
                if await native.getIndexUuid() != claim.indexUuid:
                    raise LexicalIndexError("Native index changed before writing")

                async def assertOwned() -> None:
                    await self.database.run(partial(self.repository._assertClaim, claim))

                await native.writeBuild(buildId=claim.buildId, units=payload.units, beforeBatch=assertOwned)
                await self.database.run(partial(self.repository._assertClaim, claim))
                if await native.getIndexUuid() != claim.indexUuid:
                    raise LexicalIndexError("Native index changed before publication")
                outcome: Literal["completed"] = await self._publishVisibleBuild(claim, native)
                isKnown = True
                return outcome
        except asyncio.CancelledError:
            await self._settle(context, claim, ownerToken, LexicalIndexError(
                "Indexing request handler was cancelled", isRetryable=True, hasUnknownOutcome=True))
            raise
        except Exception as error:
            failure: Exception = LexicalIndexError("Indexing execution deadline exceeded", isRetryable=True,
                hasUnknownOutcome=True, isTimeout=True) if isinstance(error, TimeoutError) else error
            isKnown = not isinstance(failure, LexicalIndexError) or not failure.hasUnknownOutcome
            return await self._settle(context, claim, ownerToken, failure)
        finally:
            if native is not None:
                native.close()
            if claim is not None:
                settlement: IndexingDeadline = self.deadline.startSettlement(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)
                self.repository._deadline = settlement
                try:
                    async with asyncio.timeout(settlement.getRemainingSeconds()):
                        await self.database.run(partial(self.repository._finishUse, claim, isKnown=isKnown))
                except Exception as error:
                    self.hasPendingSettlement = True
                    logger.error("Async indexing use remains unresolved: build_id={}, error_type={}", claim.buildId, type(error).__name__)

    async def _publishVisibleBuild(self, claim: _Claim, native: AsyncOpenSearchLexicalIndex) -> Literal["completed"]:
        publication = ManifestPublication(self.repository.sessions)
        for _ in range(8):
            prepared = await self.database.run(partial(publication.prepareMetadata, scope=claim.context.scope,
                documentId=claim.context.documentId, buildId=claim.buildId))
            await native.writePublishedManifest(manifestId=prepared.id, scopeId=prepared.scopeId,
                buildIds=prepared.buildIds)
            try:
                activation = partial(self.repository._activate, claim, prepared)
                return await self.database.run(activation)
            except ManifestPublicationAdvanced:
                continue
        raise ManifestPublicationAdvanced("Namespace manifest kept advancing during publication", isRetryable=True)

    async def _prepareInput(self, claim: _Claim) -> LexicalInput:
        source: _InputSource = await self.database.run(partial(self.repository._readInputSource, claim))
        inputs: LexicalInputStorage = LexicalInputStorage()
        if source.key:
            if source.checksum is None:
                raise LexicalIndexError("Assigned indexing input has no checksum")
            payload: LexicalInput = await inputs.loadAsync(key=source.key, checksum=source.checksum, deadline=self.deadline)
        else:
            if source.compiler != LexicalPreparation.getCompilerFingerprint():
                raise LexicalIndexError("Retained parsing input requires its original indexing compiler")
            payload = await LexicalPreparation.prepareAsync(documentId=claim.context.documentId,
                revisionId=claim.context.revisionId, sections=source.sections, chunks=source.chunks, deadline=self.deadline)
            key, checksum, byteCount = await inputs.saveAsync(jobId=claim.context.jobId,
                revisionId=claim.context.revisionId, payload=payload, deadline=self.deadline)
            await self.database.run(partial(self.repository._assignInput, claim, key, checksum, byteCount))
        await self.database.run(partial(self.repository._completeInput, claim, source, payload))
        return payload

    async def _settle(self, context: _Context | str | None, claim: _Claim | None,
                      ownerToken: str | None, error: Exception) -> Literal["completed", "pending", "failed", "skipped"]:
        self.errorCode = error.getIndexingFailureCode() if isinstance(error, LexicalIndexError) else "UNAVAILABLE"
        if not isinstance(context, _Context):
            self.hasPendingSettlement = True
            return "pending"
        settlement: IndexingDeadline = self.deadline.startSettlement(settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS)
        self.repository._deadline = settlement
        try:
            async with asyncio.timeout(settlement.getRemainingSeconds()):
                outcome = await self.database.run(partial(self.repository._recordFailure, context, claim, error, ownerToken=ownerToken))
            self.hasPendingSettlement = outcome == "pending"
            return outcome
        except Exception as settlementError:
            self.hasPendingSettlement = True
            logger.error("Async indexing failure settlement deferred: job_id={}, error_type={}", context.jobId, type(settlementError).__name__)
            return "pending"
