"""
Tier service.
Determines, caches, and refreshes a user's tier based on lifetime payment history.
"""

from __future__ import annotations

from typing import Optional

from app.services.rate_limit.config import RateLimitConfig
from app.services.rate_limit.data_structures import TierLimits
from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import redis_pool_manager
from shared.core.database import get_db_context
from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.models.database.payment_record import PaymentRecord
from shared.models.database.tier_limit import TierLimit
from shared.models.database.user_balance import UserBalance

from shared.services.billing.credits_service import CreditsService
from shared.services.redis.redis_service import RedisService

_DEFAULT_TIER: str = "free"
_USER_TIER_TTL_SECONDS: int = 3600


class TierService:
    """Manages user tier lookup, caching, and refresh."""

    @staticmethod
    async def get_tier(user_id: str) -> str:
        """Return a user's tier from cache or database.

        Missing user tier state is treated as invalid data and raises directly;
        this method never falls back to a default tier for user lookup.
        """
        redis_service = redis_pool_manager.get_redis_service()
        cached_tier = await TierService._get_cached_tier(redis_service, user_id)
        if cached_tier is not None:
            return cached_tier

        async with get_db_context() as session:
            try:
                user_tier: str = await TierService._get_tier_from_db(session, user_id)
            except NotFoundException:
                user_tier = await TierService._initialize_missing_user_tier(
                    session,
                    user_id,
                )

        await TierService._set_cached_tier(redis_service, user_id, user_tier)
        return user_tier

    @staticmethod
    async def get_tier_for_session(session: AsyncSession, user_id: str) -> str:
        """Return a user's tier using the caller-owned database session.

        This keeps request runtimes that already own a transaction from opening a
        second global DB context, while preserving the same cache and
        first-use-initialization behavior as get_tier().
        """
        redis_service = redis_pool_manager.get_redis_service()
        cached_tier = await TierService._get_cached_tier(redis_service, user_id)
        if cached_tier is not None:
            return cached_tier

        try:
            user_tier: str = await TierService._get_tier_from_db(session, user_id)
        except NotFoundException:
            user_tier = await TierService._initialize_missing_user_tier(
                session,
                user_id,
            )

        await TierService._set_cached_tier(redis_service, user_id, user_tier)
        return user_tier

    @staticmethod
    async def refresh_tier(user_id: str, session: AsyncSession) -> str:
        """Called on payment success.

        1. Sum credits_amount (micro-dollars) from payment_records WHERE status='succeeded'.
        2. Query tier_limits ordered by min_lifetime_amount_micro DESC.
        3. Pick the first tier where total >= threshold.
        4. Update user_balances.user_tier.
        5. Return the new tier name.
        """
        # Step 1: sum lifetime successful payments in micro-dollars
        stmt = (
            select(func.coalesce(func.sum(PaymentRecord.credits_amount), 0))
            .where(PaymentRecord.user_id == user_id)
            .where(PaymentRecord.status == "succeeded")
        )
        result = await session.execute(stmt)
        total_amount_micro: int = int(result.scalar_one() or 0)

        # Step 2 + 3: query tiers from DB, find highest qualifying
        tier_stmt = select(TierLimit).order_by(
            TierLimit.min_lifetime_amount_micro.desc()
        )
        tier_result = await session.execute(tier_stmt)
        tiers = tier_result.scalars().all()

        new_tier = _DEFAULT_TIER
        for tier in tiers:
            if total_amount_micro >= tier.min_lifetime_amount_micro:
                new_tier = tier.tier_name
                break

        # Step 4: persist the tier on user_balances
        # NOTE: caller is responsible for commit/rollback.
        stmt_update = (
            update(UserBalance)
            .where(UserBalance.user_id == user_id)
            .values(user_tier=new_tier)
        )
        await session.execute(stmt_update)
        await TierService._invalidate_user_tier_cache(user_id)

        logger.info(
            "Tier refreshed: user_id=%s total_micro=%d new_tier=%s",
            user_id,
            total_amount_micro,
            new_tier,
        )
        return new_tier

    @staticmethod
    def get_limits(user_tier: str) -> Optional[TierLimits]:
        """Read from in-memory tier_map via RateLimitConfig.

        Zero Redis calls.
        """
        config = RateLimitConfig.get_instance()
        return config.tier_map.get(user_tier)

    @staticmethod
    def _get_user_tier_key(user_id: str) -> str:
        """Return the Redis key for a user's rate-limit tier."""
        return f"tier:user:{user_id}"

    @staticmethod
    async def _get_tier_from_db(session: AsyncSession, user_id: str) -> str:
        """Load a user's tier from user_balances or raise when missing."""
        result = await session.execute(
            select(UserBalance.user_tier).where(UserBalance.user_id == user_id).limit(1)
        )
        user_tier = result.scalar_one_or_none()
        if user_tier is None:
            raise NotFoundException(
                resource="UserBalance",
                resource_id=user_id,
                internal_message=f"User tier not found for user_id={user_id}",
            )
        return str(user_tier)

    @staticmethod
    async def _initialize_missing_user_tier(
        session: AsyncSession,
        user_id: str,
    ) -> str:
        """Create missing first-use billing state, then return the user's tier."""
        credits_service: CreditsService = CreditsService()
        await credits_service.ensure_user_initialized(session, user_id)
        user_tier: str = await TierService._get_tier_from_db(session, user_id)
        logger.info(
            "Initialized missing user balance during tier lookup: user_id={}",
            user_id,
        )
        return user_tier

    @staticmethod
    async def _get_cached_tier(
        redis_service: RedisService,
        user_id: str,
    ) -> str | None:
        """Return cached user tier, or None on miss/cache failure."""
        cache_key = TierService._get_user_tier_key(user_id)
        try:
            cached_tier = await redis_service.get(cache_key)
        except Exception:
            logger.warning(
                "tier_service: failed to read tier cache for user_id={}",
                user_id,
            )
            return None

        return cached_tier if isinstance(cached_tier, str) else None

    @staticmethod
    async def _set_cached_tier(
        redis_service: RedisService,
        user_id: str,
        user_tier: str,
    ) -> None:
        """Cache a user's rate-limit tier."""
        try:
            await redis_service.set(
                TierService._get_user_tier_key(user_id),
                user_tier,
                ttl=_USER_TIER_TTL_SECONDS,
            )
        except Exception:
            logger.warning(
                "tier_service: failed to write tier cache for user_id={}",
                user_id,
            )

    @staticmethod
    async def _invalidate_user_tier_cache(user_id: str) -> None:
        """Invalidate cached tier data for a user."""
        try:
            await redis_pool_manager.get_redis_service().delete(
                TierService._get_user_tier_key(user_id),
            )
        except Exception:
            logger.warning(
                "Tier refresh cache invalidation failed for user_id={}",
                user_id,
            )
