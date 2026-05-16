from __future__ import annotations

import math

from app.services.rate_limit.config import (
    CONCURRENCY_RETRY_AFTER_SECONDS,
    RateLimitConfig,
)
from app.services.rate_limit.data_structures import CurrentUser, TierLimits
from app.services.rate_limit.limiter import RateLimiter
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    RateLimitException,
    UnavailableException,
)
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.database.user_balance import UserBalance

_ACTIVE_JOB_STATES: tuple[str, ...] = (
    JobStatus.WAITING_FILE.value,
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.CONVERTING.value,
)
_RETRY_AFTER_SECONDS: int = 15


class JobAdmissionCapacityService:
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
