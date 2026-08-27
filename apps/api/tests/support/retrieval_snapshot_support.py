from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.testing.contract_runtime import get_contract_database_url


@asynccontextmanager
async def contract_db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_contract_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()
