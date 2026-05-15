from __future__ import annotations

import math
from fnmatch import fnmatch

from app.services.rate_limit.config import (
    CONCURRENCY_RETRY_AFTER_SECONDS,
    RateLimitConfig,
)
from app.services.rate_limit.data_structures import CurrentUser, TierLimits
from app.services.rate_limit.limiter import RateLimiter
from app.services.rate_limit.system_limit import find_system_rule
from app.services.rate_limit.tier_service import TierService
from fastapi import Request
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    PermissionDeniedException,
    RateLimitException,
    UnavailableException,
)
from shared.core.logging import log_context
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.database.user_balance import UserBalance

_ACTIVE_JOB_STATES: tuple[str, ...] = (
    JobStatus.WAITING_FILE.value,
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.CONVERTING.value,
)
_GUEST_API_KEY_ALLOWED_ROUTE_PATTERNS: tuple[str, ...] = (
    "/v1/jobs",
    "/v1/jobs/*",
    "/v1/billing/credits",
    "/v1/retrieval/query",
    "/v1/documents",
    "/v1/documents/*",
    "/mcp",
)
_GUEST_API_KEY_REQUIRED_PERMISSION: str = (
    "jobs_documents_retrieval_mcp_or_billing_credits"
)
_GUEST_API_KEY_SCOPE_MESSAGE: str = (
    "Guest API keys can only access job, document, retrieval, MCP query, "
    "and billing credits APIs"
)
_RETRY_AFTER_SECONDS: int = 15


class JobAdmissionService:
    async def resolve_current_user(
        self,
        *,
        request: Request,
        user_id: str,
    ) -> CurrentUser:
        user_tier = await TierService.get_tier(user_id)
        self._enforce_guest_api_key_scope(request=request, user_tier=user_tier)
        current_user = CurrentUser(user_id=user_id, user_tier=user_tier)

        with log_context(user_id=user_id):
            config = RateLimitConfig.get_instance()
            if not config.is_enabled:
                return current_user

            try:
                await self._check_user_system_limit(
                    request=request,
                    config=config,
                    user_id=user_id,
                )
            except RateLimitException:
                raise
            except Exception as exc:
                logger.warning(
                    "rate_limit: Redis error during system limit check, "
                    "failing open for user_id={}, error={}",
                    user_id,
                    exc,
                )

        return current_user

    async def enforce_billing_limits(
        self,
        *,
        current_user: CurrentUser,
    ) -> None:
        if not settings.BILLING_ENABLED:
            return

        config = RateLimitConfig.get_instance()
        if not config.is_enabled:
            return

        tier_limits = self._require_tier_limits(
            config=config,
            current_user=current_user,
        )

        limiter = RateLimiter(config)
        try:
            await limiter.check_billing_rpm(
                current_user.user_id,
                tier_limits.rpm_limit,
            )
        except RateLimitException:
            raise
        except Exception as exc:
            raise UnavailableException(
                internal_message=(f"Redis error in billing RPM check: {exc}"),
                retry_after=_RETRY_AFTER_SECONDS,
            )

    async def enforce_route_system_limit(self, *, request: Request) -> None:
        config = RateLimitConfig.get_instance()
        if not config.is_enabled:
            return

        route_path = self._get_route_path(request)
        route_identifier = self._get_route_limit_identifier(request)
        rule = find_system_rule(request.method, route_path, config.system_rules)
        limiter = RateLimiter(config)

        try:
            await limiter.check_system_limit(
                identifier=route_identifier,
                limit=rule.limit,
                matched_pattern=rule.api_pattern,
                period=rule.period,
                use_global_key=True,
            )
        except RateLimitException:
            raise
        except Exception as exc:
            raise UnavailableException(
                internal_message=(f"Redis error in route system limit: {exc}"),
                retry_after=_RETRY_AFTER_SECONDS,
                limit=rule.limit,
                period=rule.period,
            )

    async def enforce_job_creation_capacity(
        self,
        *,
        db: AsyncSession,
        current_user: CurrentUser,
    ) -> None:
        if not settings.BILLING_ENABLED:
            return

        config = RateLimitConfig.get_instance()
        if not config.is_enabled:
            return

        tier_limits = self._require_tier_limits(
            config=config,
            current_user=current_user,
        )
        limiter = RateLimiter(config)

        if tier_limits.max_concurrent_jobs != -1:
            try:
                await self._acquire_user_concurrency_lock(
                    db=db,
                    user_id=current_user.user_id,
                )
                active_jobs = await self._count_non_terminal_jobs(
                    db=db,
                    user_id=current_user.user_id,
                )
                if active_jobs >= tier_limits.max_concurrent_jobs:
                    retry_after_seconds = self._compute_concurrency_retry_after_seconds(
                        base_retry_after_seconds=CONCURRENCY_RETRY_AFTER_SECONDS,
                        rpm_limit=tier_limits.rpm_limit,
                    )
                    exc = RateLimitException(
                        retry_after=retry_after_seconds,
                        limit=tier_limits.max_concurrent_jobs,
                        period="concurrent",
                        user_message=(
                            f"Too many concurrent requests "
                            f"({active_jobs}/{tier_limits.max_concurrent_jobs} active). "
                            f"Please retry after {retry_after_seconds} seconds."
                        ),
                        internal_message=(
                            "Concurrency limit exceeded: "
                            f"user_id={current_user.user_id}, "
                            f"active_jobs={active_jobs}, "
                            f"limit={tier_limits.max_concurrent_jobs}, "
                            f"retry_after={retry_after_seconds}s"
                        ),
                    )
                    exc.details.update(
                        {
                            "active_jobs": active_jobs,
                            "available_slots": max(
                                0,
                                tier_limits.max_concurrent_jobs - active_jobs,
                            ),
                        }
                    )
                    raise exc
            except RateLimitException:
                raise
            except Exception as exc:
                raise UnavailableException(
                    internal_message=(f"DB error in concurrency check: {exc}"),
                    retry_after=_RETRY_AFTER_SECONDS,
                )

        if tier_limits.daily_quota != -1:
            try:
                await limiter.check_daily_quota(
                    current_user.user_id,
                    tier_limits.daily_quota,
                )
            except RateLimitException:
                raise
            except Exception as exc:
                raise UnavailableException(
                    internal_message=(f"Redis error in daily quota check: {exc}"),
                    retry_after=_RETRY_AFTER_SECONDS,
                )

    async def _check_user_system_limit(
        self,
        *,
        request: Request,
        config: RateLimitConfig,
        user_id: str,
    ) -> None:
        route_path = self._get_route_path(request)
        rule = find_system_rule(request.method, route_path, config.system_rules)
        limiter = RateLimiter(config)
        await limiter.check_system_limit(
            identifier=user_id,
            limit=rule.limit,
            matched_pattern=rule.api_pattern,
            period=rule.period,
        )

    def _require_tier_limits(
        self,
        *,
        config: RateLimitConfig,
        current_user: CurrentUser,
    ) -> TierLimits:
        tier_limits = config.tier_map.get(current_user.user_tier)
        if tier_limits is None:
            logger.error(
                "rate_limit: no tier config for tier='{}', user_id={}",
                current_user.user_tier,
                current_user.user_id,
            )
            raise UnavailableException(
                internal_message=(
                    f"Missing tier config for tier={current_user.user_tier}"
                ),
                retry_after=_RETRY_AFTER_SECONDS,
            )
        return tier_limits

    def _get_route_path(self, request: Request) -> str:
        scope_path = request.scope.get("path", request.url.path)
        root_path = request.scope.get("root_path", "")
        if isinstance(scope_path, str) and isinstance(root_path, str):
            if root_path and scope_path.startswith(root_path):
                return scope_path[len(root_path) :]
            return scope_path
        return request.url.path

    def _get_route_limit_identifier(self, request: Request) -> str:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path:
            return route_path

        route_path_format = getattr(route, "path_format", None)
        if isinstance(route_path_format, str) and route_path_format:
            return route_path_format

        return self._get_route_path(request)

    def _normalize_route_path(self, route_path: str) -> str:
        normalized_path = route_path.rstrip("/")
        return normalized_path or "/"

    def _is_guest_api_key_route_allowed(self, route_path: str) -> bool:
        normalized_path = self._normalize_route_path(route_path)
        return any(
            fnmatch(normalized_path, pattern)
            for pattern in _GUEST_API_KEY_ALLOWED_ROUTE_PATTERNS
        )

    def _enforce_guest_api_key_scope(
        self,
        *,
        request: Request,
        user_tier: str,
    ) -> None:
        if user_tier != "guest":
            return

        route_path = self._get_route_path(request)
        if self._is_guest_api_key_route_allowed(route_path):
            return

        raise PermissionDeniedException(
            user_message=_GUEST_API_KEY_SCOPE_MESSAGE,
            required_permission=_GUEST_API_KEY_REQUIRED_PERMISSION,
        )

    async def _acquire_user_concurrency_lock(
        self,
        *,
        db: AsyncSession,
        user_id: str,
    ) -> None:
        result = await db.execute(
            select(UserBalance.user_id)
            .where(UserBalance.user_id == user_id)
            .with_for_update()
        )
        if result.scalar_one_or_none() is None:
            raise RateLimitException(
                internal_message=f"UserBalance row not found for user_id={user_id}"
            )

    async def _count_non_terminal_jobs(
        self,
        *,
        db: AsyncSession,
        user_id: str,
    ) -> int:
        result = await db.execute(
            select(func.count(Job.job_id))
            .where(Job.user_id == user_id)
            .where(Job.status.in_(_ACTIVE_JOB_STATES))
        )
        return int(result.scalar_one() or 0)

    def _compute_concurrency_retry_after_seconds(
        self,
        *,
        base_retry_after_seconds: int,
        rpm_limit: int,
    ) -> int:
        if rpm_limit <= 0:
            return base_retry_after_seconds
        return max(base_retry_after_seconds, int(math.ceil(60 / rpm_limit)))
