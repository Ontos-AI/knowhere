import importlib
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.testing.contract_runtime import (
    configure_runtime_database_role,
    PostgreSQLProcess,
    clear_application_modules,
    cleanup_contract_runtime_async,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
    seed_contract_developer,
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_API_ROOT: Path = _REPO_ROOT / "apps" / "api"
_SHARED_ROOT: Path = _REPO_ROOT / "packages" / "shared-python"


def _ensure_import_paths() -> None:
    api_root_value = str(_API_ROOT)
    shared_root_value = str(_SHARED_ROOT)

    if api_root_value not in sys.path:
        sys.path.insert(0, api_root_value)

    if shared_root_value not in sys.path:
        sys.path.insert(0, shared_root_value)


async def _create_contract_engine() -> AsyncEngine:
    return create_async_engine(get_contract_database_url(), future=True)


async def _count_jobs() -> int:
    engine = await _create_contract_engine()
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text("SELECT COUNT(*) FROM jobs"))
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _set_local_developer_tier_limits(
    *,
    max_concurrent_jobs: int = -1,
    rpm_limit: int = -1,
    daily_quota: int = -1,
) -> None:
    engine = await _create_contract_engine()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE tier_limits
                    SET max_concurrent_jobs = :max_concurrent_jobs,
                        rpm_limit = :rpm_limit,
                        daily_quota = :daily_quota
                    WHERE tier_name = :tier_name
                    """
                ),
                {
                    "max_concurrent_jobs": max_concurrent_jobs,
                    "rpm_limit": rpm_limit,
                    "daily_quota": daily_quota,
                    "tier_name": "tier_5",
                },
            )
    finally:
        await engine.dispose()


async def _set_default_system_limit(
    *,
    rpm: int,
    period: str = "minute",
) -> None:
    engine = await _create_contract_engine()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE system_limits
                    SET rpm = :rpm,
                        period = :period
                    WHERE method = :method
                      AND api_pattern = :api_pattern
                    """
                ),
                {
                    "rpm": rpm,
                    "period": period,
                    "method": "*",
                    "api_pattern": "*",
                },
            )
    finally:
        await engine.dispose()


@asynccontextmanager
async def _create_rate_limited_developer_api_client(
    monkeypatch: MonkeyPatch,
    postgresql_process: PostgreSQLProcess,
    *,
    billing_enabled: bool = True,
    rate_limit_enabled: bool = True,
    max_concurrent_jobs: int = -1,
    rpm_limit: int = -1,
    daily_quota: int = -1,
    default_system_rpm: int = 1000,
    default_system_period: str = "minute",
) -> AsyncGenerator[AsyncClient, None]:
    configure_contract_environment(monkeypatch, postgresql_process)
    monkeypatch.setenv("BILLING_ENABLED", "true" if billing_enabled else "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true" if rate_limit_enabled else "false")
    await prepare_contract_storage()
    engine = await _create_contract_engine()
    try:
        async with engine.connect() as connection:
            previousTier = (await connection.execute(text("SELECT max_concurrent_jobs, rpm_limit, daily_quota FROM tier_limits WHERE tier_name='tier_5'"))).mappings().one()
            previousSystem = (await connection.execute(text("SELECT rpm, period FROM system_limits WHERE method='*' AND api_pattern='*'"))).mappings().one()
    finally:
        await engine.dispose()
    await _set_local_developer_tier_limits(
        max_concurrent_jobs=max_concurrent_jobs,
        rpm_limit=rpm_limit,
        daily_quota=daily_quota,
    )
    await _set_default_system_limit(
        rpm=default_system_rpm,
        period=default_system_period,
    )
    await configure_runtime_database_role(monkeypatch, get_contract_database_url(postgresql_process))
    _ensure_import_paths()
    clear_application_modules()

    api_module: ModuleType = importlib.import_module("main")
    app = api_module.app

    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                developer_profile = await seed_contract_developer()
                client.headers.update(
                    {"Authorization": f"Bearer {str(developer_profile['api_key'])}"}
                )
                yield client
    finally:
        await _set_local_developer_tier_limits(**dict(previousTier))
        await _set_default_system_limit(**dict(previousSystem))
        await cleanup_contract_runtime_async(remove_test_directories=True)


def _assert_retryable_rate_limit_response(
    response: Response,
    *,
    expected_limit: int,
    expected_period: str,
) -> dict[str, object]:
    assert response.status_code == 429
    assert response.headers["x-request-id"]
    assert response.headers["x-ratelimit-limit"] == str(expected_limit)
    assert response.headers["x-ratelimit-period"] == expected_period

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    retry_after = cast(int, details["retry_after"])

    assert response_json["success"] is False
    assert error["code"] == "RESOURCE_EXHAUSTED"
    assert error["message"] == (
        f"Rate limit exceeded. Please retry after {retry_after} seconds."
    )
    assert response.headers["retry-after"] == str(retry_after)
    assert details["reason"] == "RATE_LIMIT_EXCEEDED"
    assert details["limit"] == expected_limit
    assert details["period"] == expected_period
    assert details["remaining"] == 0
    assert isinstance(details["reset"], int)

    return details


@pytest.mark.asyncio
async def test_should_return_too_many_requests_when_the_authenticated_user_exceeds_their_concurrent_job_limit(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-rate-limit-first.pdf",
        "data_id": "contract-job-rate-limit-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-rate-limit-second.pdf",
        "data_id": "contract-job-rate-limit-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        max_concurrent_jobs=1,
        rpm_limit=60,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.headers["x-request-id"]
    assert second_response.headers["retry-after"] == "30"
    assert second_response.headers["x-ratelimit-limit"] == "1"
    assert second_response.headers["x-ratelimit-period"] == "concurrent"

    response_json = cast(dict[str, object], second_response.json())
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])

    assert response_json["success"] is False
    assert error["code"] == "RESOURCE_EXHAUSTED"
    assert (
        error["message"]
        == "Too many concurrent requests (1/1 active). Please retry after 30 seconds."
    )
    assert details == {
        "reason": "RATE_LIMIT_EXCEEDED",
        "retry_after": 30,
        "limit": 1,
        "period": "concurrent",
        "active_jobs": 1,
        "available_slots": 0,
    }
    assert await _count_jobs() == 1


@pytest.mark.asyncio
async def test_should_return_too_many_requests_when_the_jobs_route_exceeds_the_system_limit(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-system-limit-first.pdf",
        "data_id": "contract-job-system-limit-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-system-limit-second.pdf",
        "data_id": "contract-job-system-limit-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        default_system_rpm=1,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    details = _assert_retryable_rate_limit_response(
        second_response,
        expected_limit=1,
        expected_period="minute",
    )

    assert cast(int, details["retry_after"]) >= 1
    assert await _count_jobs() == 1


@pytest.mark.asyncio
async def test_should_return_too_many_requests_when_the_authenticated_user_exceeds_their_billing_rpm(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-billing-rpm-first.pdf",
        "data_id": "contract-job-billing-rpm-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-billing-rpm-second.pdf",
        "data_id": "contract-job-billing-rpm-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        rpm_limit=1,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    details = _assert_retryable_rate_limit_response(
        second_response,
        expected_limit=1,
        expected_period="minute",
    )

    assert cast(int, details["retry_after"]) >= 1
    assert await _count_jobs() == 1


@pytest.mark.asyncio
async def test_should_skip_billing_rate_limits_when_billing_is_disabled(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-billing-disabled-first.pdf",
        "data_id": "contract-job-billing-disabled-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-billing-disabled-second.pdf",
        "data_id": "contract-job-billing-disabled-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        billing_enabled=False,
        max_concurrent_jobs=1,
        rpm_limit=1,
        daily_quota=1,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert await _count_jobs() == 2


@pytest.mark.asyncio
async def test_should_skip_all_api_rate_limits_when_rate_limits_are_disabled(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-rate-limits-disabled-first.pdf",
        "data_id": "contract-job-rate-limits-disabled-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-rate-limits-disabled-second.pdf",
        "data_id": "contract-job-rate-limits-disabled-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        rate_limit_enabled=False,
        max_concurrent_jobs=1,
        rpm_limit=1,
        daily_quota=1,
        default_system_rpm=1,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert await _count_jobs() == 2


@pytest.mark.asyncio
async def test_should_return_too_many_requests_when_the_authenticated_user_exceeds_their_daily_quota(
    monkeypatch: MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    first_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-daily-quota-first.pdf",
        "data_id": "contract-job-daily-quota-first",
    }
    second_payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-daily-quota-second.pdf",
        "data_id": "contract-job-daily-quota-second",
    }

    async with _create_rate_limited_developer_api_client(
        monkeypatch,
        postgresql_proc,
        daily_quota=1,
    ) as api_client:
        first_response = await api_client.post("/api/v1/jobs", json=first_payload)
        second_response = await api_client.post("/api/v1/jobs", json=second_payload)

    assert first_response.status_code == 200
    details = _assert_retryable_rate_limit_response(
        second_response,
        expected_limit=1,
        expected_period="day",
    )

    retry_after = cast(int, details["retry_after"])
    assert 1 <= retry_after <= 3600
    assert await _count_jobs() == 1
