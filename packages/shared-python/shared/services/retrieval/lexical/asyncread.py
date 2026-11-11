"""Run lock-free published reads on the API's existing async pools."""

import asyncio
from functools import partial
import time
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from shared.core.config import settings
from shared.models.database.lexicalcontrol import LexicalBackendControl
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.models.database.lexicalpublishedmanifest import LexicalPublishedManifest
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.lexical.manifest_filters import buildManifestFilters
from shared.services.retrieval.lexical.manifest_publication import MANIFEST_READ_TIMEOUT_SECONDS
from shared.services.retrieval.lexical.asynccoordination import AsyncLexicalCoordination
from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.hit import LexicalHit
from shared.services.retrieval.lexical.reader import PublishedLexicalRead
from shared.services.retrieval.lexical.recency import LexicalRecency
from shared.services.retrieval.lexical.stage_observer import StageObserver


class AsyncPublishedLexicalRead(PublishedLexicalRead):
    def __init__(self, *, userId: str, namespace: str, bind: AsyncEngine | AsyncConnection,
                 isClassic: bool = False, preferContentCoverage: bool = False) -> None:
        self.database: AsyncLexicalDatabase = AsyncLexicalDatabase(bind)
        super().__init__(userId=userId, namespace=namespace,
            sessionFactory=self.database.sessions, isClassic=isClassic)
        self._asyncCoordination: AsyncLexicalCoordination = AsyncLexicalCoordination(self.database)
        self._asyncNative: AsyncOpenSearchLexicalIndex | None = None
        self.preferContentCoverage: bool = preferContentCoverage
        self._manifestDeadline: float | None = None

    async def startAsync(self) -> None:
        await self.database.run(self._captureRead)

    async def startManifestAsync(
        self, *, documentScope: DocumentScope, chunkTypes: set[str] | None, topK: int,
    ) -> bool:
        started: float = time.monotonic()
        storage: CorpusStorage = CorpusStorage.resolve_namespace(self.scope.namespace)
        document = storage.Document
        chunk = storage.DocumentChunk
        documentFilters = (
            document.user_id == self.scope.ownerId, document.namespace == self.scope.namespace,
            document.status == "active", documentScope.predicate(document.document_id),
        )
        chunkQuery = select(chunk.id).join(document,
            (chunk.document_id == document.document_id) & (chunk.job_result_id == document.current_job_result_id)
        ).where(*documentFilters)
        if chunkTypes is not None:
            chunkQuery = chunkQuery.where(func.lower(chunk.chunk_type).in_(chunkTypes))
        chunkCount = select(func.count()).select_from(chunkQuery.limit(topK + 1).subquery()).scalar_subquery()
        generation = storage.RetrievalNamespaceGeneration
        currentGeneration = select(generation.generation).where(
            generation.user_id == self.scope.ownerId, generation.namespace == self.scope.namespace,
        ).scalar_subquery()
        query = select(LexicalNamespaceIndex.publishedManifestId, LexicalNamespaceIndex.indexUuid,
            LexicalNamespaceIndex.indexName, LexicalBackendControl.epoch).join(LexicalBackendControl,
                LexicalBackendControl.id == 1).join(LexicalPublishedManifest,
                LexicalPublishedManifest.id == LexicalNamespaceIndex.publishedManifestId).where(
            LexicalNamespaceIndex.id == self.scope.fingerprint,
            LexicalPublishedManifest.namespaceIndexId == self.scope.fingerprint,
            LexicalPublishedManifest.state == "active",
            LexicalPublishedManifest.generation == func.coalesce(currentGeneration, 0),
            LexicalBackendControl.backend == "opensearch",
            LexicalBackendControl.mode == "serving", chunkCount > topK,
        )
        if settings.VISUAL_RETRIEVAL_ENABLED and (
            chunkTypes is None or "page" in chunkTypes or chunkTypes.issubset({"image", "table"})
        ):
            embedding = storage.DocumentPageEmbedding
            hasEmbeddings = select(embedding.document_id).join(document,
                (embedding.document_id == document.document_id)
                & (embedding.job_result_id == document.current_job_result_id)
            ).where(*documentFilters).exists()
            query = query.where(~hasEmbeddings)
        async with AsyncSession(self.database.engine) as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            return False
        manifestId, indexUuid, indexName, epoch = row
        native = AsyncOpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
            scope=self.scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
            preferContentCoverage=self.preferContentCoverage)
        if not manifestId or not manifestId.startswith(self.scope.fingerprint + "-") or not indexUuid or indexName != native.indexName:
            raise LexicalIndexError("Published manifest has no matching native index")
        self.manifestId = manifestId
        self.epoch = epoch
        self._indexUuid = indexUuid
        self._asyncNative = native
        self._manifestDeadline = started + MANIFEST_READ_TIMEOUT_SECONDS
        self._hasStarted = True
        return True

    async def searchManifestAsync(
        self, *, queryTokens: tuple[str, ...], limit: int, documentScope: DocumentScope,
        chunkTypes: set[str] | None, excludeSections: list[dict[str, str]],
    ) -> tuple[LexicalHit, ...]:
        self.assertAlive()
        if self.manifestId is None or self._manifestDeadline is None or self._asyncNative is None:
            raise LexicalIndexError("A published manifest was not selected")
        if documentScope.include == frozenset():
            return ()
        filters, exclusions = buildManifestFilters(scope=documentScope, chunkTypes=chunkTypes,
            excludeSections=excludeSections)
        lookup = {"index": f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-published-manifests",
            "id": self.manifestId, "path": "buildIds"}
        LexicalRecency.recordAccess(self.scope.fingerprint)
        try:
            async with asyncio.timeout(max(0, self._manifestDeadline - time.monotonic())):
                try:
                    return await self._asyncNative.search(buildIds=(), queryTokens=queryTokens,
                        limit=limit, buildLookup=lookup, extraFilters=filters, exclusions=exclusions)
                except LexicalIndexError as error:
                    if not error.isIndexClosed:
                        raise
                    indexUuid, epoch = await self._asyncCoordination.ensureIndex(
                        scope=self.scope, native=self._asyncNative)
                    self._validateRecovery(indexUuid, epoch)
                    return await self._asyncNative.search(buildIds=(), queryTokens=queryTokens,
                        limit=limit, buildLookup=lookup, extraFilters=filters, exclusions=exclusions)
        except TimeoutError as error:
            raise LexicalIndexError("Published manifest read exceeded its deadline", isRetryable=True, isTimeout=True) from error

    async def assertAliveAsync(self) -> None:
        self.assertAlive()

    async def searchAsync(self, *, buildIds: tuple[str, ...], queryTokens: tuple[str, ...],
                          limit: int, unitIds: tuple[str, ...] | None = None,
                          sectionIds: tuple[str, ...] | None = None,
                          extraFilters: tuple[dict[str, object], ...] = (),
                          exclusions: tuple[dict[str, object], ...] = ()) -> tuple[LexicalHit, ...]:
        self.assertAlive()
        if not buildIds or not queryTokens or unitIds == () or sectionIds == ():
            return ()
        observationStart: float = time.perf_counter()
        StageObserver.add("lexical_executions")
        isCold: bool = False
        LexicalRecency.recordAccess(self.scope.fingerprint)
        if self._asyncNative is None:
            self._asyncNative = AsyncOpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                scope=self.scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
                preferContentCoverage=self.preferContentCoverage)
        search = self._asyncNative.search
        if sectionIds is not None or extraFilters or exclusions:
            search = partial(search, sectionIds=sectionIds, extraFilters=extraFilters, exclusions=exclusions)
        try:
            hits: tuple[LexicalHit, ...] = await search(buildIds=buildIds,
                queryTokens=queryTokens, limit=limit, unitIds=unitIds)
        except LexicalIndexError as error:
            if not error.isIndexClosed:
                raise
            isCold = True
            indexUuid, epoch = await self._asyncCoordination.ensureIndex(scope=self.scope, native=self._asyncNative)
            self._validateRecovery(indexUuid, epoch)
            hits = await search(buildIds=buildIds, queryTokens=queryTokens, limit=limit, unitIds=unitIds)
        self._validateHits(hits)
        elapsed: float = (time.perf_counter() - observationStart) * 1000
        StageObserver.add("lexical_ms", elapsed)
        StageObserver.add("wait_to_discovery_ms", elapsed)
        if isCold:
            StageObserver.add("cold_first_ms", elapsed)
        return hits

    async def finishAsync(self, db: AsyncSession | None = None) -> None:
        await self.closeAsync()

    async def closeAsync(self) -> None:
        self.close()
        if self._asyncNative is not None:
            self._asyncNative.close()
