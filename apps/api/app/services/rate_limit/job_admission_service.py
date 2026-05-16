from __future__ import annotations

from app.services.rate_limit.config import RateLimitConfig
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.job_admission_capacity_service import (
    JobAdmissionCapacityService,
)
from app.services.rate_limit.job_admission_route_policy_service import (
    JobAdmissionRoutePolicyService,
)
from app.services.rate_limit.tier_service import TierService
from fastapi import Request
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
        request: Request,
        user_id: str,
    ) -> CurrentUser:
        user_tier = await TierService.get_tier(user_id)
        self._route_policy_service.enforce_guest_api_key_scope(
            request=request,
            user_tier=user_tier,
        )
        current_user = CurrentUser(user_id=user_id, user_tier=user_tier)

        with log_context(user_id=user_id):
            config = RateLimitConfig.get_instance()
            if not config.is_enabled:
                return current_user

            try:
                await self._route_policy_service.enforce_user_system_limit(
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
        await self._capacity_service.enforce_billing_limits(current_user=current_user)

    async def enforce_route_system_limit(self, *, request: Request) -> None:
        await self._route_policy_service.enforce_route_system_limit(request=request)

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
