"""Short PostgreSQL snapshots for the classic retrieval read phase."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from shared.services.retrieval.execution.revision_pins import (
    RetrievalRevisionPins,
    is_revision_generation_stable,
)

_SNAPSHOT_ACTIVE: str = "retrieval_postgresql_snapshot"


class PostgreSQLServingGenerationChanged(RuntimeError):
    """The planned PostgreSQL publication changed before the read began."""


@asynccontextmanager
async def open_postgresql_snapshot(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Read committed publications without changing a caller's open transaction."""
    if db.info.get(_SNAPSHOT_ACTIVE):
        yield db
        return

    bind: AsyncEngine | AsyncConnection | None = db.bind
    session: AbstractAsyncContextManager[AsyncSession] = nullcontext(db)
    if db.in_transaction() or (
        isinstance(bind, AsyncConnection) and bind.in_transaction()
    ):
        if isinstance(bind, AsyncConnection):
            bind = bind.engine
        session = AsyncSession(bind=bind)

    async with session as snapshot, snapshot.begin():
        await snapshot.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        snapshot.info[_SNAPSHOT_ACTIVE] = True
        try:
            yield snapshot
        finally:
            snapshot.info.pop(_SNAPSHOT_ACTIVE)


async def validate_postgresql_snapshot(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    pins: RetrievalRevisionPins | None,
) -> None:
    if pins is not None and not await is_revision_generation_stable(
        db, user_id=user_id, namespace=namespace, pins=pins, strict=True,
    ):
        raise PostgreSQLServingGenerationChanged
