from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

import fakeredis
import fakeredis.aioredis
import psycopg2
import redis as redis_sync
import redis.asyncio as redis_asyncio
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

CONTRACT_DATABASE_NAME: str = "Knowhere_contract_test"
DEFAULT_POSTGRESQL_PORT: int = 5432
CONTRACT_POSTGRESQL_PORT_RANGE: tuple[int, int] = (15432, 25432)
CONTRACT_REDIS_DATABASE: int = 14
CONTRACT_REDIS_HOST: str = "127.0.0.1"
CONTRACT_REDIS_PORT: int = 6379
CONTRACT_WEBHOOK_MASTER_KEY: str = "".join(
    [
        "GE5FgAG9t4a1C1xTRNiOC2",
        "GQHgp4YMSN7t8lTJq-FxY=",
    ]
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_API_ROOT: Path = _REPO_ROOT / "apps" / "api"
_SHARED_ROOT: Path = _REPO_ROOT / "packages" / "shared-python"
_TEST_TMP_ROOT: Path = Path("/tmp/knowhere-api-tests")
_STATIC_TABLES_TO_PRESERVE: frozenset[str] = frozenset(
    {
        "alembic_version",
        "tier_limits",
        "system_limits",
    }
)
_MODULE_NAMES_TO_CLEAR: tuple[str, ...] = (
    "main",
    "custom_openapi",
    "shared.core.config",
    "shared.core.config.app",
    "shared.core.config.base",
    "shared.core.config.billing",
    "shared.core.config.celery",
    "shared.core.config.database",
    "shared.core.config.job",
    "shared.core.config.qstash",
    "shared.core.config.redis",
    "shared.core.config.storage",
    "shared.core.database",
    "shared.core.database_sync",
    "shared.core.state_machine.service_sync",
    "shared.services.billing.credits_sync_service",
    "shared.services.billing.work_billing_service",
    "shared.services.jobs.lifecycle.service",
    "shared.services.retrieval.app_service",
    "shared.services.retrieval.publication_service",
    "shared.services.webhook",
    "shared.services.webhook.qstash_publisher",
    "scripts.init_user",
)
CONTRACT_DEVELOPER_USER_ID: str = "local-dev-user"
CONTRACT_DEVELOPER_USER_NAME: str = "Local Development User"
CONTRACT_DEVELOPER_USER_EMAIL: str = "local-dev-user@knowhere.local"
CONTRACT_DEVELOPER_USER_TIER: str = "tier_5"
CONTRACT_DEVELOPER_API_KEY_NAME: str = "contract-developer-api-key"
_contract_storage_prepared: bool = False
_contract_storage_database_url: str | None = None
_contract_fake_redis_server: fakeredis.FakeServer = fakeredis.FakeServer()


class PostgreSQLProcess(Protocol):
    @property
    def host(self) -> str: ...

    @property
    def port(self) -> int: ...

    @property
    def user(self) -> str: ...

    @property
    def password(self) -> str | None: ...


def _ensure_import_paths() -> None:
    api_root_value: str = str(_API_ROOT)
    shared_root_value: str = str(_SHARED_ROOT)

    if api_root_value not in sys.path:
        sys.path.insert(0, api_root_value)

    if shared_root_value not in sys.path:
        sys.path.insert(0, shared_root_value)


def _ensure_test_directories() -> None:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def _reset_contract_storage_state(database_url: str) -> None:
    global _contract_storage_database_url
    global _contract_storage_prepared

    if _contract_storage_database_url == database_url:
        return

    _contract_storage_database_url = database_url
    _contract_storage_prepared = False


def _ensure_contract_postgresql_port(database_url: URL) -> None:
    effective_port = database_url.port or DEFAULT_POSTGRESQL_PORT

    if effective_port == DEFAULT_POSTGRESQL_PORT:
        raise RuntimeError(
            "Contract tests must not use port 5432. Use the pytest-postgresql "
            "process fixture on CONTRACT_POSTGRESQL_PORT_RANGE instead."
        )


def _resolve_base_database_url(
    postgresql_process: PostgreSQLProcess | None = None,
) -> URL:
    if postgresql_process is not None:
        database_url = URL.create(
            "postgresql+asyncpg",
            username=postgresql_process.user,
            password=postgresql_process.password,
            host=postgresql_process.host,
            port=postgresql_process.port,
            database="postgres",
        )
        _ensure_contract_postgresql_port(database_url)
        return database_url

    configured_url: str | None = os.getenv("DATABASE_URL")

    if configured_url is None:
        raise RuntimeError(
            "API tests require a pytest-postgresql process or an explicit "
            "DATABASE_URL. Use the postgresql_proc fixture for tests, or set "
            "DATABASE_URL before calling configure_contract_environment()."
        )

    database_url = make_url(configured_url)
    _ensure_contract_postgresql_port(database_url)
    return database_url


def get_contract_database_url(
    postgresql_process: PostgreSQLProcess | None = None,
) -> str:
    contract_database_url: URL = _resolve_base_database_url(postgresql_process).set(
        database=CONTRACT_DATABASE_NAME
    )
    return contract_database_url.render_as_string(hide_password=False)


def _get_contract_sync_database_url() -> str:
    sync_database_url: URL = make_url(get_contract_database_url()).set(
        drivername="postgresql"
    )
    return sync_database_url.render_as_string(hide_password=False)


def _get_admin_sync_database_url() -> str:
    admin_database_url: URL = make_url(_get_contract_sync_database_url()).set(
        database="postgres"
    )
    return admin_database_url.render_as_string(hide_password=False)


def _build_admin_sync_database_url(contract_database_url: str) -> str:
    sync_database_url = make_url(contract_database_url).set(drivername="postgresql")
    admin_database_url = sync_database_url.set(database="postgres")
    return admin_database_url.render_as_string(hide_password=False)


def _reset_redis_service_factory() -> None:
    redis_factory_module = sys.modules.get("shared.services.redis.redis_service_factory")
    redis_package_module = sys.modules.get("shared.services.redis")
    redis_service_factory = None

    if redis_factory_module is not None:
        redis_service_factory = getattr(redis_factory_module, "RedisServiceFactory", None)
    elif redis_package_module is not None:
        redis_service_factory = getattr(redis_package_module, "RedisServiceFactory", None)

    redis_reset = getattr(redis_service_factory, "reset", None)
    if callable(redis_reset):
        redis_reset()

    sync_redis_module = sys.modules.get("shared.services.redis.redis_sync_service")
    if sync_redis_module is None:
        return

    sync_redis_service_factory = getattr(
        sync_redis_module,
        "SyncRedisServiceFactory",
        None,
    )
    sync_redis_reset = getattr(sync_redis_service_factory, "reset", None)

    if callable(sync_redis_reset):
        sync_redis_reset()


class ContractFakeAsyncRedis(fakeredis.aioredis.FakeRedis):
    def __init__(
        self,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("decode_responses", True)
        redis_kwargs: dict[str, Any] = cast(dict[str, Any], kwargs)
        super().__init__(
            *args,
            server=_contract_fake_redis_server,
            **redis_kwargs,
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *args: object,
        **kwargs: object,
    ) -> fakeredis.aioredis.FakeRedis:
        kwargs.setdefault("decode_responses", True)
        redis_kwargs: dict[str, Any] = cast(dict[str, Any], kwargs)
        return fakeredis.aioredis.FakeRedis.from_url(
            url,
            *args,
            server=_contract_fake_redis_server,
            **redis_kwargs,
        )


class ContractFakeSyncRedis(fakeredis.FakeRedis):
    def __init__(
        self,
        *args: object,
        **kwargs: object,
    ) -> None:
        kwargs.pop("connection_pool", None)
        kwargs.setdefault("decode_responses", True)
        redis_kwargs: dict[str, Any] = cast(dict[str, Any], kwargs)
        super().__init__(
            *args,
            server=_contract_fake_redis_server,
            **redis_kwargs,
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *args: object,
        **kwargs: object,
    ) -> fakeredis.FakeRedis:
        kwargs.setdefault("decode_responses", True)
        redis_kwargs: dict[str, Any] = cast(dict[str, Any], kwargs)
        return fakeredis.FakeRedis.from_url(
            url,
            *args,
            server=_contract_fake_redis_server,
            **redis_kwargs,
        )


def _configure_contract_redis(monkeypatch: MonkeyPatch) -> None:
    global _contract_fake_redis_server

    _contract_fake_redis_server = fakeredis.FakeServer()
    monkeypatch.setattr(redis_asyncio, "Redis", ContractFakeAsyncRedis)
    monkeypatch.setattr(redis_asyncio, "from_url", ContractFakeAsyncRedis.from_url)
    monkeypatch.setattr(redis_sync, "Redis", ContractFakeSyncRedis)
    monkeypatch.setattr(redis_sync, "from_url", ContractFakeSyncRedis.from_url)
    _reset_redis_service_factory()


def configure_contract_environment(
    monkeypatch: MonkeyPatch,
    postgresql_process: PostgreSQLProcess | None = None,
) -> None:
    _ensure_test_directories()
    database_url: str = get_contract_database_url(postgresql_process)
    _reset_contract_storage_state(database_url)

    environment: dict[str, str] = {
        "ENVIRONMENT": "production",
        "API_STANDALONE_MODE_ENABLED": "true",
        "WEBHOOK_MASTER_KEY": CONTRACT_WEBHOOK_MASTER_KEY,
        "DATABASE_URL": database_url,
        "DB_SSL_MODE": "disable",
        "DB_USE_NULL_POOL": "true",
        "REDIS_HOST": CONTRACT_REDIS_HOST,
        "REDIS_PORT": str(CONTRACT_REDIS_PORT),
        "REDIS_DATABASE": str(CONTRACT_REDIS_DATABASE),
        "CELERY_REDIS_URL": (
            f"redis://{CONTRACT_REDIS_HOST}:{CONTRACT_REDIS_PORT}/{CONTRACT_REDIS_DATABASE}"
        ),
        "TMP_PATH": str(_TEST_TMP_ROOT),
        "S3_BUCKET_NAME": "knowhere-test-bucket",
        "S3_ACCESS_KEY_ID": "test-access-key",
        "S3_SECRET_ACCESS_KEY": "test-secret-key",
        "S3_TEMP_PATH": str(_TEST_TMP_ROOT),
        "S3_ENDPOINT_URL": "http://127.0.0.1:4566",
        "S3_PRIVATE_DOMAIN": "http://127.0.0.1:4566",
        "S3_RESULTS_BUCKET": "knowhere-test-results",
        "S3_REGION": "us-west-1",
        "S3_USE_SSL": "false",
        "S3_ADDRESSING_STYLE": "path",
        "STRIPE_SECRET_KEY": "sk_test_contract_secret",
        "STRIPE_WEBHOOK_SECRET": "whsec_contract_test_secret",
        "DS_KEY": "test-deepseek-key",
        "DS_URL": "https://example.com/v1",
        "QSTASH_CURRENT_SIGNING_KEY": "qstash-current-test-key",
        "QSTASH_NEXT_SIGNING_KEY": "qstash-next-test-key",
        "QSTASH_CALLBACK_BASE_URL": "http://localhost:5005/api/v1",
    }

    if "BILLING_ENABLED" not in os.environ:
        environment["BILLING_ENABLED"] = "true"

    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    clear_application_modules()
    _configure_contract_redis(monkeypatch)


def clear_application_modules() -> None:
    cached_module_names: list[str] = list(sys.modules)

    for module_name in cached_module_names:
        if module_name in _MODULE_NAMES_TO_CLEAR:
            sys.modules.pop(module_name, None)
            continue

        if module_name == "shared.services.redis" or module_name.startswith(
            "shared.services.redis."
        ):
            sys.modules.pop(module_name, None)
            continue

        if module_name == "shared.services.storage" or module_name.startswith(
            "shared.services.storage."
        ):
            sys.modules.pop(module_name, None)
            continue

        if module_name == "shared.services.jobs" or module_name.startswith(
            "shared.services.jobs."
        ):
            sys.modules.pop(module_name, None)
            continue

        if module_name == "shared.services.webhook" or module_name.startswith(
            "shared.services.webhook."
        ):
            sys.modules.pop(module_name, None)
            continue

        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)


def _dispose_sync_database_engine() -> None:
    database_sync_module = sys.modules.get("shared.core.database_sync")

    if database_sync_module is None:
        return

    sync_engine = getattr(database_sync_module, "_sync_engine", None)
    if sync_engine is not None:
        sync_engine.dispose()

    setattr(database_sync_module, "_sync_engine", None)
    setattr(database_sync_module, "_sync_session_factory", None)


async def _dispose_async_database_engine() -> None:
    database_module = sys.modules.get("shared.core.database")

    if database_module is None:
        return

    async_engine = cast(AsyncEngine | None, getattr(database_module, "engine", None))

    if async_engine is not None:
        await async_engine.dispose()


async def _close_async_redis_service_factory() -> None:
    redis_factory_module = sys.modules.get("shared.services.redis.redis_service_factory")
    redis_package_module = sys.modules.get("shared.services.redis")
    redis_service_factory = None

    if redis_factory_module is not None:
        redis_service_factory = getattr(redis_factory_module, "RedisServiceFactory", None)
    elif redis_package_module is not None:
        redis_service_factory = getattr(redis_package_module, "RedisServiceFactory", None)

    close_current_service = cast(
        Callable[[], Awaitable[None]] | None,
        getattr(
            redis_service_factory,
            "close_current_service",
            None,
        ),
    )

    if close_current_service is not None:
        await close_current_service()


async def _close_redis_pool_manager() -> None:
    config_module = sys.modules.get("shared.core.config")
    config_app_module = sys.modules.get("shared.core.config.app")
    redis_pool_manager = None

    if config_module is not None:
        redis_pool_manager = getattr(config_module, "redis_pool_manager", None)
    elif config_app_module is not None:
        redis_pool_manager = getattr(config_app_module, "redis_pool_manager", None)

    close_pool = cast(
        Callable[[], Awaitable[None]] | None,
        getattr(redis_pool_manager, "close_pool", None),
    )

    if close_pool is not None:
        await close_pool()


def _reset_rate_limit_config() -> None:
    rate_limit_module = sys.modules.get("app.services.rate_limit.config")

    if rate_limit_module is None:
        return

    rate_limit_config = getattr(rate_limit_module, "RateLimitConfig", None)
    reset_instance = getattr(rate_limit_config, "reset_instance", None)

    if callable(reset_instance):
        reset_instance()


def _reset_fake_redis_server() -> None:
    global _contract_fake_redis_server

    _contract_fake_redis_server = fakeredis.FakeServer()


def _cleanup_contract_runtime_sync(*, remove_test_directories: bool) -> None:
    _reset_rate_limit_config()
    _reset_redis_service_factory()
    _dispose_sync_database_engine()
    _reset_fake_redis_server()
    clear_application_modules()

    if remove_test_directories:
        shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True)


async def cleanup_contract_runtime_async(
    *,
    remove_test_directories: bool = False,
) -> None:
    await _close_redis_pool_manager()
    await _close_async_redis_service_factory()
    await _dispose_async_database_engine()
    _cleanup_contract_runtime_sync(remove_test_directories=remove_test_directories)


def cleanup_contract_runtime(*, remove_test_directories: bool = False) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(
            cleanup_contract_runtime_async(
                remove_test_directories=remove_test_directories,
            )
        )
        return

    _cleanup_contract_runtime_sync(remove_test_directories=remove_test_directories)


def _ensure_contract_database_exists() -> None:
    contract_database_name: str = make_url(get_contract_database_url()).database or ""

    connection = psycopg2.connect(_get_admin_sync_database_url())
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (contract_database_name,),
            )
            database_exists: bool = cursor.fetchone() is not None

            if not database_exists:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(contract_database_name)
                    )
                )
    finally:
        connection.close()


def _recreate_contract_database() -> None:
    contract_database_url = get_contract_database_url()
    contract_database_name = make_url(contract_database_url).database

    if contract_database_name is None:
        raise RuntimeError("Contract database URL does not include a database name.")

    _drop_database(
        database_name=contract_database_name,
        admin_database_url=_build_admin_sync_database_url(contract_database_url),
    )
    _ensure_contract_database_exists()


def _initialize_contract_database() -> None:
    contract_database_name: str = make_url(get_contract_database_url()).database or ""

    admin_connection = psycopg2.connect(_get_admin_sync_database_url())
    admin_connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER DATABASE {} SET search_path TO public").format(
                    sql.Identifier(contract_database_name)
                )
            )
    finally:
        admin_connection.close()

    contract_connection = psycopg2.connect(_get_contract_sync_database_url())
    contract_connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    try:
        with contract_connection.cursor() as cursor:
            cursor.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            cursor.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
            cursor.execute("SET timezone = 'UTC'")
    finally:
        contract_connection.close()


def _drop_database(*, database_name: str, admin_database_url: str) -> None:
    connection = psycopg2.connect(admin_database_url)
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s
                  AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )
    finally:
        connection.close()


def drop_contract_database(
    postgresql_process: PostgreSQLProcess | None = None,
) -> None:
    global _contract_storage_database_url
    global _contract_storage_prepared

    contract_database_url = get_contract_database_url(postgresql_process)
    contract_database_name = make_url(contract_database_url).database

    if contract_database_name is None:
        raise RuntimeError("Contract database URL does not include a database name.")

    _drop_database(
        database_name=contract_database_name,
        admin_database_url=_build_admin_sync_database_url(contract_database_url),
    )

    if _contract_storage_database_url == contract_database_url:
        _contract_storage_database_url = None
        _contract_storage_prepared = False


def _run_contract_migrations() -> None:
    migration_environment = os.environ.copy()
    python_path_entries = [
        str(_API_ROOT),
        str(_SHARED_ROOT),
        migration_environment.get("PYTHONPATH", ""),
    ]
    migration_environment.update(
        {
            "API_STANDALONE_MODE_ENABLED": "true",
            "DATABASE_URL": get_contract_database_url(),
            "DB_SSL_MODE": "disable",
            "PYTHONPATH": os.pathsep.join(
                entry for entry in python_path_entries if entry
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=str(_API_ROOT),
        capture_output=True,
        text=True,
        env=migration_environment,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Contract database migration failed: "
            f"{result.stderr or result.stdout or 'unknown error'}"
        )


def _assert_contract_schema_ready() -> None:
    connection = psycopg2.connect(_get_contract_sync_database_url())

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename IN ('tier_limits', 'api_keys', 'user_balances')
                """
            )
            migrated_tables = {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()

    expected_tables = {"tier_limits", "api_keys", "user_balances"}
    missing_tables = expected_tables - migrated_tables
    if missing_tables:
        raise RuntimeError(
            "Contract database migration did not create expected tables: "
            f"{', '.join(sorted(missing_tables))}"
        )


async def _create_contract_engine() -> AsyncEngine:
    return create_async_engine(get_contract_database_url(), future=True)


async def prepare_contract_storage() -> None:
    global _contract_storage_prepared

    _ensure_import_paths()

    if not _contract_storage_prepared:
        _recreate_contract_database()
        _initialize_contract_database()
        _run_contract_migrations()
        _assert_contract_schema_ready()
        _contract_storage_prepared = True

    await reset_contract_database()
    await reset_contract_redis()


async def seed_contract_developer() -> dict[str, str | int]:
    _ensure_import_paths()

    init_user_module: ModuleType = importlib.import_module("scripts.init_user")
    initialize_standalone_user = getattr(
        init_user_module,
        "initialize_standalone_user",
    )

    try:
        initialized_user = await initialize_standalone_user(
            email=CONTRACT_DEVELOPER_USER_EMAIL,
            user_id=CONTRACT_DEVELOPER_USER_ID,
            name=CONTRACT_DEVELOPER_USER_NAME,
            key_name=CONTRACT_DEVELOPER_API_KEY_NAME,
            tier=CONTRACT_DEVELOPER_USER_TIER,
        )
    finally:
        await _dispose_async_database_engine()

    return {
        "user_id": initialized_user["user_id"],
        "name": CONTRACT_DEVELOPER_USER_NAME,
        "email": initialized_user["email"],
        "tier": CONTRACT_DEVELOPER_USER_TIER,
        "api_key": initialized_user["api_key"],
    }


async def reset_contract_database() -> None:
    engine: AsyncEngine = await _create_contract_engine()

    try:
        async with engine.begin() as connection:
            table_names_result = await connection.execute(
                text(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                    ORDER BY tablename
                    """
                )
            )
            table_names: list[str] = [
                row[0]
                for row in table_names_result.fetchall()
                if row[0] not in _STATIC_TABLES_TO_PRESERVE
            ]

            if table_names:
                qualified_table_list: str = ", ".join(
                    f'public."{table_name}"' for table_name in table_names
                )
                await connection.execute(
                    text(
                        f"TRUNCATE TABLE {qualified_table_list} RESTART IDENTITY CASCADE"
                    )
                )
    finally:
        await engine.dispose()


async def reset_contract_redis() -> None:
    redis_client = redis_asyncio.Redis(
        host=CONTRACT_REDIS_HOST,
        port=CONTRACT_REDIS_PORT,
        db=CONTRACT_REDIS_DATABASE,
        decode_responses=True,
    )

    try:
        await redis_client.flushdb()
    finally:
        await redis_client.aclose()
