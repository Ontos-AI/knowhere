from __future__ import annotations

from app.services.rate_limit.config import RateLimitConfig
from app.services.rate_limit.data_structures import CurrentUser, RouteAdmissionContext
from app.services.rate_limit.job_admission_capacity_service import (
    JobAdmissionCapacityService,
)
from app.services.rate_limit.job_admission_route_policy_service import (
    JobAdmissionRoutePolicyService,
)
from app.services.rate_limit.tier_service import TierService
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import RateLimitException
from shared.core.logging import log_context


class JobAdmissionService:
    def __init__(
        self,
        *,
        route_policy_service: JobAdmissionRoutePolicyService | None = None,
        capacity_service: JobAdmissionCapacityService | None = None,
    ) -> None:
        self._route_policy_service = (
            route_policy_service or JobAdmissionRoutePolicyService()
        )
        self._capacity_service = capacity_service or JobAdmissionCapacityService()

    async def resolve_current_user(
        self,
        *,
        route_context: RouteAdmissionContext,
        user_id: str,
    ) -> CurrentUser:
        user_tier = await TierService.get_tier(user_id)
        self._route_policy_service.enforce_guest_api_key_scope(
            route_context=route_context,
            user_tier=user_tier,
        )
        current_user = CurrentUser(user_id=user_id, user_tier=user_tier)

        with log_context(user_id=user_id):
            config = RateLimitConfig.get_instance()
            if not config.is_enabled:
                return current_user

            try:
                await self._route_policy_service.enforce_user_system_limit(
                    route_context=route_context,
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
        await self._capacity_service.enforce_billing_limits(current_user=current_user)

    async def enforce_route_system_limit(
        self,
        *,
        route_context: RouteAdmissionContext,
    ) -> None:
        await self._route_policy_service.enforce_route_system_limit(
            route_context=route_context,
        )

    async def enforce_job_creation_capacity(
        self,
        *,
        db: AsyncSession,
        current_user: CurrentUser,
    ) -> None:
        await self._capacity_service.enforce_job_creation_capacity(
            db=db,
            current_user=current_user,
        )
