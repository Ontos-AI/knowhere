import importlib
import os
import sys
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import ModuleType

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_postgresql import factories
from pytest import MonkeyPatch
from tests.support.import_environment import configure_import_environment, ensure_import_paths
from shared.testing.contract_runtime import (
    CONTRACT_POSTGRESQL_PORT_RANGE,
    PostgreSQLProcess,
    clear_application_modules,
    cleanup_contract_runtime,
    cleanup_contract_runtime_async,
    configure_contract_environment,
    drop_contract_database,
    prepare_contract_storage,
    seed_contract_developer,
)
from shared.testing.postgresql_environment import (
    configure_pytest_postgresql_for_current_platform,
    find_executable,
)

configure_pytest_postgresql_for_current_platform()

configure_import_environment()
ensure_import_paths()

_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_API_ROOT: Path = _REPO_ROOT / "apps" / "api"
_SHARED_ROOT: Path = _REPO_ROOT / "packages" / "shared-python"


def _resolve_postgresql_executable() -> str | None:
    configured_executable: str | None = os.getenv("PYTEST_POSTGRESQL_EXECUTABLE")

    if configured_executable:
        return configured_executable

    executable_path: Path | None = find_executable("pg_ctl")
    return str(executable_path) if executable_path is not None else None


_contract_postgresql_proc = factories.postgresql_proc(
    executable=_resolve_postgresql_executable(),
    port=CONTRACT_POSTGRESQL_PORT_RANGE,
)


@pytest.fixture(scope="session")
def postgresql_proc(
    _contract_postgresql_proc: PostgreSQLProcess,
) -> Generator[PostgreSQLProcess, None, None]:
    try:
        yield _contract_postgresql_proc
    finally:
        cleanup_contract_runtime(remove_test_directories=True)
        drop_contract_database(_contract_postgresql_proc)


def _ensure_import_paths() -> None:
    api_root_value: str = str(_API_ROOT)
    shared_root_value: str = str(_SHARED_ROOT)

    if api_root_value not in sys.path:
        sys.path.insert(0, api_root_value)

    if shared_root_value not in sys.path:
        sys.path.insert(0, shared_root_value)


async def _load_api_module(
    monkeypatch: MonkeyPatch,
    postgresql_process: PostgreSQLProcess,
) -> ModuleType:
    configure_contract_environment(monkeypatch, postgresql_process)
    await prepare_contract_storage()
    _ensure_import_paths()
    clear_application_modules()
    return importlib.import_module("main")


@asynccontextmanager
async def _create_api_client(
    monkeypatch: MonkeyPatch,
    postgresql_process: PostgreSQLProcess,
) -> AsyncGenerator[AsyncClient, None]:
    try:
        api_module: ModuleType = await _load_api_module(monkeypatch, postgresql_process)
        app = api_module.app

        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                yield client
    finally:
        await cleanup_contract_runtime_async(remove_test_directories=True)


@pytest.fixture
def api_client_factory(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> Generator[Callable[[], AbstractAsyncContextManager[AsyncClient]], None, None]:
    def create_api_client() -> AbstractAsyncContextManager[AsyncClient]:
        return _create_api_client(monkeypatch, postgresql_proc)

    try:
        yield create_api_client
    finally:
        cleanup_contract_runtime(remove_test_directories=True)


@asynccontextmanager
async def _create_developer_api_client(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> AsyncGenerator[AsyncClient, None]:
    async with api_client_factory() as api_client:
        developer_profile: dict[str, str | int] = await seed_contract_developer()
        api_client.headers.update(
            {"Authorization": f"Bearer {str(developer_profile['api_key'])}"}
        )
        yield api_client


@pytest.fixture
def developer_api_client_factory(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> Callable[[], AbstractAsyncContextManager[AsyncClient]]:
    def create_developer_api_client() -> AbstractAsyncContextManager[AsyncClient]:
        return _create_developer_api_client(api_client_factory)

    return create_developer_api_client
