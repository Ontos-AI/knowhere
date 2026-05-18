"""Redis-backed rate-limiting service."""

import os
import time
from typing import Any, Dict

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.utils.redis_key_builder import redis_key_builder


class RateLimitService:
    """Redis-backed rate-limit service."""

    # Default values, overridable through environment variables.
    RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # 60-second window.
    RATE_LIMIT_MAX_REQUESTS = int(
        os.getenv("RATE_LIMIT_MAX_REQUESTS", "1000")
    )  # Maximum request count.
    RATE_LIMIT_ENABLED = (
        os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    )  # Whether rate limiting is enabled.

    def __init__(self, redis_service: RedisService):
        self.redis = redis_service
        logger.info(
            f"Rate limit service initialized: "
            f"enabled={self.RATE_LIMIT_ENABLED}, "
            f"window={self.RATE_LIMIT_WINDOW}s, "
            f"max_requests={self.RATE_LIMIT_MAX_REQUESTS}"
        )

    async def check_rate_limit(self, user_id: str, api_name: str) -> Dict[str, Any]:
        """
        Check and update the rate limit state.

        Args:
            user_id: User ID.
            api_name: API name.

        Returns:
            {
                "allowed": bool,  # Whether the request is allowed.
                "limit": int,  # Request limit.
                "remaining": int,  # Remaining request count.
                "reset": int,  # Reset timestamp.
            }
        """
        # Allow everything when rate limiting is disabled.
        if not self.RATE_LIMIT_ENABLED:
            return {
                "allowed": True,
                "limit": self.RATE_LIMIT_MAX_REQUESTS,
                "remaining": self.RATE_LIMIT_MAX_REQUESTS,
                "reset": int(time.time()) + self.RATE_LIMIT_WINDOW,
            }

        try:
            # Build the rate-limit key.
            rate_limit_key = redis_key_builder.rate_limit_api(user_id, api_name)

            # Use a pipeline to keep the operations atomic.
            client = await self.redis._get_client()
            async with client.pipeline() as pipe:
                # Increment the counter.
                await pipe.incr(rate_limit_key)

                # Set TTL on the first increment.
                await pipe.expire(rate_limit_key, self.RATE_LIMIT_WINDOW)

                # Read back the current count and TTL.
                await pipe.get(rate_limit_key)
                await pipe.ttl(rate_limit_key)

                # Execute the pipeline.
                results = await pipe.execute()

            current_count = int(results[0])  # INCR result.
            ttl_seconds = int(results[2])  # TTL result.

            # Compute remaining requests.
            remaining = max(0, self.RATE_LIMIT_MAX_REQUESTS - current_count)

            # Compute the reset timestamp.
            reset_timestamp = int(time.time()) + ttl_seconds

            # Decide whether the request is still allowed.
            allowed = current_count <= self.RATE_LIMIT_MAX_REQUESTS

            rate_limit_info = {
                "allowed": allowed,
                "limit": self.RATE_LIMIT_MAX_REQUESTS,
                "remaining": remaining,
                "reset": reset_timestamp,
            }

            logger.debug(
                f"Rate limit check: user_id={user_id}, api={api_name}, count={current_count}, remaining={remaining}"
            )

            return rate_limit_info

        except Exception as e:
            logger.error(f"Rate limit check failed: {e}")
            # Fail open on errors, but keep the error in logs.
            return {
                "allowed": True,
                "limit": self.RATE_LIMIT_MAX_REQUESTS,
                "remaining": self.RATE_LIMIT_MAX_REQUESTS,
                "reset": int(time.time()) + self.RATE_LIMIT_WINDOW,
            }

    async def get_rate_limit_info(self, user_id: str, api_name: str) -> Dict[str, Any]:
        """
        Get the current rate-limit state without incrementing it.

        Args:
            user_id: User ID.
            api_name: API name.

        Returns:
            Rate-limit information.
        """
        try:
            rate_limit_key = redis_key_builder.rate_limit_api(user_id, api_name)

            # Load the current counter and TTL.
            current_count = await self.redis.get(rate_limit_key, 0)
            client = await self.redis._get_client()
            ttl_seconds = await client.ttl(rate_limit_key)

            if ttl_seconds == -1:
                # The key exists without a TTL; restore the default TTL.
                await self.redis.expire(rate_limit_key, self.RATE_LIMIT_WINDOW)
                ttl_seconds = self.RATE_LIMIT_WINDOW
            elif ttl_seconds == -2:
                # The key does not exist yet.
                current_count = 0
                ttl_seconds = self.RATE_LIMIT_WINDOW

            current_count = int(current_count)
            remaining = max(0, self.RATE_LIMIT_MAX_REQUESTS - current_count)
            reset_timestamp = int(time.time()) + ttl_seconds

            return {
                "allowed": current_count < self.RATE_LIMIT_MAX_REQUESTS,
                "limit": self.RATE_LIMIT_MAX_REQUESTS,
                "remaining": remaining,
                "reset": reset_timestamp,
            }

        except Exception as e:
            logger.error(f"Failed to get rate limit info: {e}")
            return {
                "allowed": True,
                "limit": self.RATE_LIMIT_MAX_REQUESTS,
                "remaining": self.RATE_LIMIT_MAX_REQUESTS,
                "reset": int(time.time()) + self.RATE_LIMIT_WINDOW,
            }
