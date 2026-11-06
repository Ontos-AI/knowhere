"""Run lock-free published reads on the API's existing async pools."""

import time
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from shared.core.config import settings
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

    async def startAsync(self) -> None:
        await self.database.run(self._captureRead)

    async def assertAliveAsync(self) -> None:
        self.assertAlive()

    async def searchAsync(self, *, buildIds: tuple[str, ...], queryTokens: tuple[str, ...],
                          limit: int, unitIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        self.assertAlive()
        if not buildIds or not queryTokens or unitIds == ():
            return ()
        observationStart: float = time.perf_counter()
        StageObserver.add("lexical_executions")
        isCold: bool = False
        LexicalRecency.recordAccess(self.scope.fingerprint)
        if self._asyncNative is None:
            self._asyncNative = AsyncOpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
                scope=self.scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX,
                preferContentCoverage=self.preferContentCoverage)
        try:
            hits: tuple[LexicalHit, ...] = await self._asyncNative.search(buildIds=buildIds,
                queryTokens=queryTokens, limit=limit, unitIds=unitIds)
        except LexicalIndexError as error:
            if not error.isIndexClosed:
                raise
            isCold = True
            indexUuid, epoch = await self._asyncCoordination.ensureIndex(scope=self.scope, native=self._asyncNative)
            self._validateRecovery(indexUuid, epoch)
            hits = await self._asyncNative.search(buildIds=buildIds,
                queryTokens=queryTokens, limit=limit, unitIds=unitIds)
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
