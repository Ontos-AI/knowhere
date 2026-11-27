"""
Sync database engine for Celery worker (gevent pool).
Uses psycopg2 in gevent green mode; the worker installs the wait callback,
with a per-statement deadline, in shared.core.statement_deadline.
API service continues using the async engine in database.py.
"""

from contextlib import contextmanager
from threading import RLock
from typing import Generator

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings

_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker | None = None
_sync_lock = RLock()


def _on_connect(dbapi_connection, connection_record) -> None:
    logger.debug("New sync database connection established")


def _on_invalidate(dbapi_connection, connection_record, exception):
    logger.warning(f"Sync database connection invalidated: {exception}")


def _build_sync_url() -> str:
    """Build the sync SQLAlchemy URL for worker-side psycopg2 usage."""
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")


def get_sync_engine() -> Engine:
    """Return the worker sync engine, creating it lazily on first actual DB use."""
    global _sync_engine

    if _sync_engine is not None:
        return _sync_engine

    with _sync_lock:
        if _sync_engine is not None:
            return _sync_engine

        engine = create_engine(
            _build_sync_url(),
            pool_size=settings.DB_SYNC_POOL_SIZE,
            max_overflow=settings.DB_SYNC_MAX_OVERFLOW,
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_pre_ping=True,
            connect_args=settings.get_ssl_connect_args(),
        )
        event.listen(engine, "connect", _on_connect)
        event.listen(engine, "invalidate", _on_invalidate)

        logger.info(
            f"Sync database engine created "
            f"(pool_size={settings.DB_SYNC_POOL_SIZE}, max_overflow={settings.DB_SYNC_MAX_OVERFLOW})"
        )
        _sync_engine = engine
        return _sync_engine


def get_sync_session_factory() -> sessionmaker:
    """Return the worker sync session factory, creating it lazily with the engine."""
    global _sync_session_factory

    if _sync_session_factory is not None:
        return _sync_session_factory

    with _sync_lock:
        if _sync_session_factory is not None:
            return _sync_session_factory

        _sync_session_factory = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
        )
        return _sync_session_factory


@contextmanager
def get_sync_db_context() -> Generator[Session, None, None]:
    """Sync database session context manager for worker tasks."""
    session = get_sync_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        logger.warning("Sync database session rollback due to exception")
        session.rollback()
        raise
    finally:
        session.close()
