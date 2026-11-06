"""Run the shared SQL-only decisions on the request's existing async pool."""

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, sessionmaker

_Result = TypeVar("_Result")


class AsyncLexicalDatabase:
    def __init__(self, bind: AsyncEngine | AsyncConnection) -> None:
        self.engine: AsyncEngine = bind if isinstance(bind, AsyncEngine) else bind.engine
        self.sessions: sessionmaker[Session] = sessionmaker(
            bind=self.engine.sync_engine, expire_on_commit=False)

    async def run(self, operation: Callable[[], _Result]) -> _Result:
        # These legacy decisions own short Session transactions. Binding them
        # to sync_engine keeps the same asyncpg driver and pool. run_sync supplies
        # SQLAlchemy's greenlet adaptation; no executor or second pool is used.
        def invoke(_session: Session) -> _Result:
            return operation()

        async with AsyncSession(self.engine) as bridge:
            return await bridge.run_sync(invoke)
