"""Database configuration."""

from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine


class DatabaseConfig(BaseModel):
    """Database configuration."""

    # Core database settings.
    DATABASE_URL: str = Field(..., description="Database connection URL")

    # SSL configuration.
    DB_SSL_MODE: str = Field(
        default="prefer",
        description="Database SSL mode (disable, allow, prefer, require, verify-ca, verify-full)",
    )
    DB_SSL_CERT: Optional[str] = Field(default=None, description="SSL certificate path")
    DB_SSL_KEY: Optional[str] = Field(default=None, description="SSL private-key path")
    DB_SSL_ROOT_CERT: Optional[str] = Field(
        default=None, description="SSL root-certificate path"
    )

    # Async database pool configuration for the API.
    # Defaults sized for legal job-poll bursts after nested-checkout hygiene
    # (pool_size + max_overflow = 100 checkouts per API process).
    DB_POOL_SIZE: int = Field(default=50, description="Connection-pool size")
    DB_MAX_OVERFLOW: int = Field(default=50, description="Maximum overflow connections")
    DB_POOL_RECYCLE: int = Field(
        default=1800, description="Connection recycle interval in seconds"
    )
    DB_POOL_TIMEOUT: int = Field(
        default=30, description="Connection checkout timeout in seconds"
    )

    # Worker sync database pool config (psycopg2, for gevent worker)
    DB_SYNC_POOL_SIZE: int = Field(default=5, description="Worker sync pool size")
    DB_SYNC_MAX_OVERFLOW: int = Field(
        default=5, description="Worker sync pool max overflow"
    )

    # Worker concurrency (gevent greenlets)
    WORKER_CONCURRENCY: int = Field(
        default=50, description="Celery gevent worker concurrency"
    )

    def get_ssl_connect_args(self) -> dict:
        """Return SSL connect args for psycopg2."""
        ssl_args = {"sslmode": self.DB_SSL_MODE}

        if self.DB_SSL_CERT:
            ssl_args["sslcert"] = self.DB_SSL_CERT
        if self.DB_SSL_KEY:
            ssl_args["sslkey"] = self.DB_SSL_KEY
        if self.DB_SSL_ROOT_CERT:
            ssl_args["sslrootcert"] = self.DB_SSL_ROOT_CERT

        return ssl_args

    def get_async_ssl_connect_args(self) -> dict:
        """Return SSL connect args for asyncpg."""
        ssl_args = {}

        if self.DB_SSL_MODE and self.DB_SSL_MODE != "disable":
            # asyncpg supports two styles:
            # 1. Simple mode: ssl=True/False
            # 2. Advanced mode: ssl=<ssl_context>
            if self.DB_SSL_MODE in ["prefer", "require"] and not self.DB_SSL_ROOT_CERT:
                # Simple mode without certificate verification.
                ssl_args["ssl"] = True
            else:
                # Advanced mode with explicit SSL context handling.
                import ssl

                # Build the SSL context.
                ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

                if self.DB_SSL_MODE == "require":
                    # require: force SSL without verifying the peer certificate.
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                elif self.DB_SSL_MODE == "verify-ca":
                    # verify-ca: validate the CA certificate.
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_REQUIRED
                    if self.DB_SSL_ROOT_CERT:
                        ssl_context.load_verify_locations(self.DB_SSL_ROOT_CERT)
                elif self.DB_SSL_MODE == "verify-full":
                    # verify-full: validate the CA certificate and hostname.
                    ssl_context.check_hostname = True
                    ssl_context.verify_mode = ssl.CERT_REQUIRED
                    if self.DB_SSL_ROOT_CERT:
                        ssl_context.load_verify_locations(self.DB_SSL_ROOT_CERT)
                else:  # prefer
                    # prefer: prefer SSL but do not require peer verification.
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

                # Load client certificates when configured.
                if self.DB_SSL_CERT and self.DB_SSL_KEY:
                    ssl_context.load_cert_chain(self.DB_SSL_CERT, self.DB_SSL_KEY)

                ssl_args["ssl"] = ssl_context

        return ssl_args

    def validate_database_config(self) -> bool:
        """Validate the database configuration by opening a connection."""
        try:
            # Reuse the synchronous URL path for a direct validation check.
            sync_url = self.DATABASE_URL.replace("asyncpg", "psycopg2")
            ssl_args = self.get_ssl_connect_args()
            engine = create_engine(sync_url, connect_args=ssl_args)
            engine.connect()
            logger.info("Database connection validation succeeded")
            return True
        except Exception as e:
            logger.error(f"Database connection validation failed: {e}")
            return False
