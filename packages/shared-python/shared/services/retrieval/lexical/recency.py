"""Coalesce native access hints; persist them outside retrieval requests."""

import asyncio
from datetime import datetime, timezone
from threading import RLock
from typing import ClassVar

from loguru import logger
from sqlalchemy import DateTime, String, column, func, update, values
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Values

from shared.models.database.lexicalnamespace import LexicalNamespaceIndex


class LexicalRecency:
    _pending: ClassVar[dict[str, datetime]] = {}
    _lock: ClassVar[RLock] = RLock()
    _MAX_PENDING: ClassVar[int] = 4096
    _FLUSH_SECONDS: ClassVar[float] = 10

    @classmethod
    def recordAccess(cls, indexId: str) -> None:
        with cls._lock:
            cls._pending.pop(indexId, None)
            cls._pending[indexId] = datetime.now(timezone.utc)
            if len(cls._pending) > cls._MAX_PENDING:
                cls._pending.pop(next(iter(cls._pending)))

    @classmethod
    def flushAccess(cls, db: Session) -> None:
        with cls._lock:
            pending: list[tuple[str, datetime]] = list(cls._pending.items())
            cls._pending.clear()
        if not pending:
            return
        recent: Values = values(column("identity", String),
            column("used_at", DateTime(timezone=True))).data(pending).alias("recent_access")
        # Recency is an eviction hint, not a lifecycle fence. Never change an
        # opening/closing operation's timestamp or move lastUsedAt backwards.
        db.execute(update(LexicalNamespaceIndex).where(
            LexicalNamespaceIndex.id == recent.c.identity,
            LexicalNamespaceIndex.state == "open",
        ).values(lastUsedAt=func.greatest(LexicalNamespaceIndex.lastUsedAt, recent.c.used_at)))

    @classmethod
    async def runFlushes(cls, bind: AsyncEngine) -> None:
        while True:
            await asyncio.sleep(cls._FLUSH_SECONDS)
            try:
                async with AsyncSession(bind) as db, db.begin():
                    await db.run_sync(cls.flushAccess)
            except Exception as error:
                # Losing hints can cause an unnecessary reopen, not mixed
                # revisions or lost data. The next batch continues normally.
                logger.warning("Lexical recency flush deferred: error_type={}", type(error).__name__)
