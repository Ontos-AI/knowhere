"""Regression tests: job-poll auth must not open nested DB checkouts."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)

# Defer importing apps/api `app` until test bodies run. Module-level imports
# would cache API's `app` package and break apps/worker contract collection in
# the same pytest process (both packages are named `app`).
configure_import_environment()
ensure_import_paths()


def _load_api_modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    ensure_import_paths()
    from app.services.auth import api_key_authentication_service
    from app.services.rate_limit import (
        data_structures,
        job_admission_service,
        tier_service,
    )

    return (
        api_key_authentication_service,
        data_structures,
        job_admission_service,
        tier_service,
    )


@pytest.mark.asyncio
async def test_get_tier_reuses_provided_session_without_get_db_context() -> None:
    _, _, _, tier_service = _load_api_modules()
    TierService = tier_service.TierService

    session = AsyncMock()
    redis_service = AsyncMock()
    redis_service.get = AsyncMock(return_value=None)
    redis_service.set = AsyncMock()

    with (
        patch(
            "app.services.rate_limit.tier_service.redis_pool_manager.get_redis_service",
            return_value=redis_service,
        ),
        patch(
            "app.services.rate_limit.tier_service.get_db_context",
        ) as get_db_context_mock,
        patch.object(
            TierService,
            "_get_tier_from_db",
            new=AsyncMock(return_value="pro"),
        ) as get_tier_from_db,
    ):
        tier = await TierService.get_tier("user-1", session=session)

    assert tier == "pro"
    get_tier_from_db.assert_awaited_once_with(session, "user-1")
    get_db_context_mock.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_current_user_passes_request_session_to_get_tier() -> None:
    _, data_structures, job_admission_service, tier_service = _load_api_modules()
    RouteAdmissionContext = data_structures.RouteAdmissionContext
    JobAdmissionService = job_admission_service.JobAdmissionService
    TierService = tier_service.TierService

    session = AsyncMock()
    route_context = RouteAdmissionContext(
        method="GET",
        path="/v1/jobs/job_abc",
        limit_identifier="GET:/v1/jobs/{job_id}",
    )
    service = JobAdmissionService(
        route_policy_service=MagicMock(
            enforce_guest_api_key_scope=MagicMock(),
            enforce_user_system_limit=AsyncMock(),
        ),
    )

    with (
        patch.object(
            TierService,
            "get_tier",
            new=AsyncMock(return_value="free"),
        ) as get_tier,
        patch(
            "app.services.rate_limit.job_admission_service.RateLimitConfig.get_instance",
            return_value=SimpleNamespace(is_enabled=False),
        ),
    ):
        current_user = await service.resolve_current_user(
            route_context=route_context,
            user_id="user-1",
            db=session,
        )

    assert current_user.user_id == "user-1"
    assert current_user.user_tier == "free"
    get_tier.assert_awaited_once_with("user-1", session=session)


@pytest.mark.asyncio
async def test_validate_api_key_updates_last_used_on_same_session() -> None:
    api_key_authentication_service, _, _, _ = _load_api_modules()
    APIKeyAuthenticationService = (
        api_key_authentication_service.APIKeyAuthenticationService
    )

    session = AsyncMock()
    session.commit = AsyncMock()
    redis_service = AsyncMock()
    redis_service.get = AsyncMock(return_value=None)
    redis_service.set_nx = AsyncMock(return_value=True)
    redis_service.set = AsyncMock()
    redis_service.sadd = AsyncMock()
    redis_service.ttl = AsyncMock(return_value=-2)
    redis_service.expire = AsyncMock()

    api_key_record = SimpleNamespace(
        id="key-1",
        user_id="user-1",
        expires_at=None,
        is_valid=lambda: True,
    )
    repository = MagicMock()
    repository.get_by_key_hash = AsyncMock(return_value=api_key_record)
    repository.update_last_used = AsyncMock(return_value=True)

    service = APIKeyAuthenticationService(repository=repository)

    with (
        patch(
            "app.services.auth.api_key_authentication_service.redis_pool_manager.get_redis_service",
            return_value=redis_service,
        ),
        patch(
            "app.services.auth.api_key_authentication_service.hash_api_key",
            return_value="hash-1",
        ),
        patch("asyncio.create_task") as create_task_mock,
    ):
        user_id = await service.validate_api_key(session, "kw_test_key")

    assert user_id == "user-1"
    repository.update_last_used.assert_awaited_once_with(session, "key-1")
    session.commit.assert_awaited_once()
    create_task_mock.assert_not_called()
    redis_service.set_nx.assert_awaited_once_with(
        "api-key:last-used-debounce:key-1",
        "1",
        ex=300,
    )


@pytest.mark.asyncio
async def test_validate_api_key_skips_last_used_when_debounced() -> None:
    api_key_authentication_service, _, _, _ = _load_api_modules()
    APIKeyAuthenticationService = (
        api_key_authentication_service.APIKeyAuthenticationService
    )

    session = AsyncMock()
    session.commit = AsyncMock()
    redis_service = AsyncMock()
    redis_service.get = AsyncMock(return_value=None)
    redis_service.set_nx = AsyncMock(return_value=False)
    redis_service.set = AsyncMock()
    redis_service.sadd = AsyncMock()
    redis_service.ttl = AsyncMock(return_value=-2)
    redis_service.expire = AsyncMock()

    api_key_record = SimpleNamespace(
        id="key-1",
        user_id="user-1",
        expires_at=None,
        is_valid=lambda: True,
    )
    repository = MagicMock()
    repository.get_by_key_hash = AsyncMock(return_value=api_key_record)
    repository.update_last_used = AsyncMock(return_value=True)

    service = APIKeyAuthenticationService(repository=repository)

    with (
        patch(
            "app.services.auth.api_key_authentication_service.redis_pool_manager.get_redis_service",
            return_value=redis_service,
        ),
        patch(
            "app.services.auth.api_key_authentication_service.hash_api_key",
            return_value="hash-1",
        ),
    ):
        user_id = await service.validate_api_key(session, "kw_test_key")

    assert user_id == "user-1"
    repository.update_last_used.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_job_poll_auth_path_uses_single_session_factory_checkout() -> None:
    """End-to-end hygiene: tier + last-used must not call get_db_context."""
    (
        api_key_authentication_service,
        data_structures,
        job_admission_service,
        tier_service,
    ) = _load_api_modules()
    APIKeyAuthenticationService = (
        api_key_authentication_service.APIKeyAuthenticationService
    )
    RouteAdmissionContext = data_structures.RouteAdmissionContext
    JobAdmissionService = job_admission_service.JobAdmissionService
    TierService = tier_service.TierService

    session = AsyncMock()
    session.commit = AsyncMock()
    checkout_count = {"n": 0}

    class _CountingContext:
        async def __aenter__(self) -> Any:
            checkout_count["n"] += 1
            return AsyncMock()

        async def __aexit__(self, *args: object) -> None:
            return None

    redis_service = AsyncMock()
    redis_service.get = AsyncMock(side_effect=[None, None])  # api-key miss, tier miss
    redis_service.set_nx = AsyncMock(return_value=True)
    redis_service.set = AsyncMock()
    redis_service.sadd = AsyncMock()
    redis_service.ttl = AsyncMock(return_value=-2)
    redis_service.expire = AsyncMock()

    api_key_record = SimpleNamespace(
        id="key-1",
        user_id="user-1",
        expires_at=None,
        is_valid=lambda: True,
    )
    repository = MagicMock()
    repository.get_by_key_hash = AsyncMock(return_value=api_key_record)
    repository.update_last_used = AsyncMock(return_value=True)
    auth_service = APIKeyAuthenticationService(repository=repository)

    route_context = RouteAdmissionContext(
        method="GET",
        path="/v1/jobs/job_abc",
        limit_identifier="GET:/v1/jobs/{job_id}",
    )
    admission = JobAdmissionService(
        route_policy_service=MagicMock(
            enforce_guest_api_key_scope=MagicMock(),
            enforce_user_system_limit=AsyncMock(),
        ),
    )

    with (
        patch(
            "app.services.auth.api_key_authentication_service.redis_pool_manager.get_redis_service",
            return_value=redis_service,
        ),
        patch(
            "app.services.rate_limit.tier_service.redis_pool_manager.get_redis_service",
            return_value=redis_service,
        ),
        patch(
            "app.services.auth.api_key_authentication_service.hash_api_key",
            return_value="hash-1",
        ),
        patch(
            "app.services.rate_limit.tier_service.get_db_context",
            side_effect=_CountingContext,
        ),
        patch.object(
            TierService,
            "_get_tier_from_db",
            new=AsyncMock(return_value="free"),
        ),
        patch(
            "app.services.rate_limit.job_admission_service.RateLimitConfig.get_instance",
            return_value=SimpleNamespace(is_enabled=False),
        ),
        patch("asyncio.create_task") as create_task_mock,
    ):
        user_id = await auth_service.validate_api_key(session, "kw_test_key")
        current_user = await admission.resolve_current_user(
            route_context=route_context,
            user_id=user_id or "",
            db=session,
        )

    assert current_user.user_id == "user-1"
    assert checkout_count["n"] == 0
    create_task_mock.assert_not_called()
    repository.update_last_used.assert_awaited_once_with(session, "key-1")
