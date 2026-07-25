import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Awaitable, Callable, TypeVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool, QueuePool

from shared.core.config import settings
from shared.core.constants import ProcessingConstants

logger = logging.getLogger(__name__)

# Get SSL connection parameters.
ssl_connect_args = settings.get_async_ssl_connect_args()
is_async_database_pool_disabled: bool = (
    os.getenv("DB_USE_NULL_POOL", "false").lower() == "true"
)
engine_options: dict[str, Any] = {
    "pool_recycle": settings.DB_POOL_RECYCLE,
    "pool_pre_ping": ProcessingConstants.DB_POOL_PRE_PING,
    "pool_reset_on_return": ProcessingConstants.DB_POOL_RESET_ON_RETURN,
    "connect_args": {
        "server_settings": {
            "application_name": "knowhere_api",
            "timezone": "UTC",
            "statement_timeout": "30000",
            "idle_in_transaction_session_timeout": "60000",
        },
        "command_timeout": 30,
        **ssl_connect_args,
    },
    "pool_events": [],
}

if is_async_database_pool_disabled:
    engine_options["poolclass"] = NullPool
else:
    engine_options.update(
        {
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_timeout": settings.DB_POOL_TIMEOUT,
        }
    )

engine = create_async_engine(
    settings.DATABASE_URL,
    **engine_options,
)
# Create the async session factory.
AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,  # Keep ORM objects usable after commit.
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Get a database session with the SQLAlchemy 2.0 async-with pattern.

    Uses AsyncSession as context manager for automatic cleanup.
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Create an app-level context manager for operations without an injected DB session.
@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    session: AsyncSession | None = None
    try:
        session = AsyncSessionFactory()
        yield session
        try:
            await session.commit()
        except Exception as commit_error:
            # Roll back if commit fails.
            try:
                if session.is_active:
                    await session.rollback()
            except Exception as rollback_error:
                # Log rollback failures only to avoid cross-event-loop connection work.
                logger.warning(f"Database session rollback failed: {rollback_error}")
            raise commit_error
    except Exception:
        # Attempt rollback if the session exists and is still active.
        if session:
            try:
                if session.is_active:
                    await session.rollback()
            except Exception as rollback_error:
                # Log rollback failures only to avoid cross-event-loop connection work.
                logger.warning(
                    f"Database session rollback failed during exception handling: {rollback_error}"
                )
            raise
    finally:
        # Close the session safely.
        if session:
            try:
                await session.close()
            except Exception as close_error:
                # Log close failures only to avoid cross-event-loop connection work.
                logger.warning(f"Database session close failed: {close_error}")


# Helper for DB work when a session cannot be passed in directly.
T = TypeVar("T")


async def db_operation(operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """
    Execute a database operation via a managed session.
    """
    async with get_db_context() as db:
        return await operation(db)


Base = declarative_base()


async def create_tables():
    """Create all database tables."""
    async with engine.begin() as conn:
        # Import all models to ensure they are registered.
        pass

        # Create all tables.
        await conn.run_sync(Base.metadata.create_all)


# Database retry helpers.
class DatabaseRetryManager:
    """Database retry manager."""

    def __init__(
        self, max_retries: int = 3, retry_delay: float = 1.0, backoff_factor: float = 2.0
    ):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor

    async def execute_with_retry(
        self,
        operation: Callable[..., Awaitable[T]],
        *args: object,
        **kwargs: object,
    ) -> T:
        """Execute a database operation with retries."""
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return await operation(*args, **kwargs)
            except Exception as e:
                last_exception = e
                logger.warning(
                    f"Database operation failed (attempt {attempt + 1}/{self.max_retries + 1}): {e}"
                )

                if attempt < self.max_retries:
                    delay = self.retry_delay * (self.backoff_factor**attempt)
                    logger.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logger.error("All retry attempts failed for database operation")
                    raise last_exception

        raise RuntimeError("Database retry manager exhausted without raising") from (
            last_exception
        )


# Shared retry-manager instance.
db_retry_manager = DatabaseRetryManager()


async def safe_db_operation(
    operation: Callable[..., Awaitable[T]], *args: object, **kwargs: object
) -> T:
    """Run a database operation through the retry manager."""
    return await db_retry_manager.execute_with_retry(operation, *args, **kwargs)


# Connection-pool event listeners.
def setup_pool_event_listeners():
    """Register connection-pool event listeners."""

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_connection, connection_record):
        """Handle new connection events."""
        logger.info("New database connection established")

    @event.listens_for(engine.sync_engine.pool, "checkout")
    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        """Handle connection checkout events and surface pool pressure."""
        pool = engine.sync_engine.pool
        if not isinstance(pool, QueuePool):
            logger.debug("Connection checked out from pool")
            return
        checked_out = pool.checkedout()
        overflow = pool.overflow()
        pool_size = pool.size()
        logger.debug(
            "Connection checked out from pool "
            "(checkedout=%s overflow=%s pool_size=%s)",
            checked_out,
            overflow,
            pool_size,
        )
        # QueuePool does not expose a first-party wait-started hook; treat
        # checkedout >= pool_size (overflow in use) as pressure / likely wait.
        if checked_out >= pool_size:
            logger.warning(
                "Database pool under pressure: checkedout=%s overflow=%s "
                "pool_size=%s max_overflow=%s (checkout waits may exceed 1s)",
                checked_out,
                overflow,
                pool_size,
                settings.DB_MAX_OVERFLOW,
            )

    @event.listens_for(engine.sync_engine.pool, "checkin")
    def on_checkin(dbapi_connection, connection_record):
        """Handle connection check-in events."""
        logger.debug("Connection checked in to pool")

    @event.listens_for(engine.sync_engine, "invalidate")
    def on_invalidate(dbapi_connection, connection_record, exception):
        """Handle connection invalidation events."""
        logger.warning(f"Database connection invalidated: {exception}")


setup_pool_event_listeners()


# Connection-pool prewarming.
async def prewarm_connection_pool():
    """Prewarm the connection pool by opening connections early."""
    if not ProcessingConstants.DB_POOL_PREWARM:
        return

    logger.info("Starting connection pool prewarming...")
    try:
        # Warm the base connection pool.
        connections_to_warm = min(settings.DB_POOL_SIZE, 5)
        tasks = []

        for _ in range(connections_to_warm):
            task = asyncio.create_task(_warm_connection())
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            f"Connection pool prewarming completed. Warmed {connections_to_warm} connections."
        )

    except Exception as e:
        logger.warning(f"Connection pool prewarming failed: {e}")


async def _warm_connection():
    """Warm a single connection."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(ProcessingConstants.DB_VALIDATION_QUERY))
    except Exception as e:
        logger.debug(f"Connection warming failed: {e}")


async def safe_dispose_engine(db_engine: AsyncEngine) -> None:
    """
    Close a database engine safely.

    Args:
        db_engine: SQLAlchemy async engine instance.
    """
    try:
        await db_engine.dispose()
        logger.info("Database engine shut down safely")
    except Exception as e:
        logger.error(f"Error while shutting down the database engine: {e}")
        # Suppress dispose failures to avoid blocking app shutdown.
