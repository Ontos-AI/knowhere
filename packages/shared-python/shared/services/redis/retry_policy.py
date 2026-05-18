"""Retry helpers for Redis operations."""

import asyncio
import random
from typing import Any, Callable

from loguru import logger

from shared.core.exceptions.redis_exceptions import (
    RedisConnectionError,
    RedisOperationError,
    RedisTimeoutError,
)


class RedisRetry:
    """Retry helper for Redis operations."""

    @staticmethod
    async def with_retry(
        operation: Callable,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ) -> Any:
        """
        Execute a Redis operation with retry support.

        Args:
            operation: Operation callback to execute.
            max_retries: Maximum retry count.
            base_delay: Base delay in seconds.
            max_delay: Maximum delay in seconds.
            exponential_base: Exponential backoff base.
            jitter: Whether to add random jitter.

        Returns:
            Operation result.

        Raises:
            RedisOperationError: Raised when all retries are exhausted.
        """
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                return await operation()
            except (RedisConnectionError, RedisOperationError, RedisTimeoutError) as e:
                last_exception = e

                if attempt == max_retries:
                    logger.error(
                        f"Redis operation failed after {max_retries} retries: {e}"
                    )
                    raise RedisOperationError(
                        internal_message=f"Redis operation failed after {max_retries} retries: {str(e)}",
                        original_exception=e,
                    )

                # Compute the retry delay.
                delay = min(base_delay * (exponential_base**attempt), max_delay)

                # Add random jitter when enabled.
                if jitter:
                    delay *= 0.5 + random.random() * 0.5

                logger.warning(
                    f"Redis operation failed, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                await asyncio.sleep(delay)

        # Should not reach here
        raise RedisOperationError(
            internal_message=f"Redis operation failed: {str(last_exception)}",
            original_exception=last_exception,
        )

    @staticmethod
    async def with_circuit_breaker(
        operation: Callable,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type = Exception,
    ) -> Any:
        """
        Execute a Redis operation with a circuit-breaker placeholder.

        Args:
            operation: Operation callback to execute.
            failure_threshold: Failure threshold.
            recovery_timeout: Recovery timeout.
            expected_exception: Expected exception type.

        Returns:
            Operation result.
        """
        # Circuit-breaker logic can be implemented here later.
        # For now, execute the operation directly.
        return await operation()


class RedisHealthChecker:
    """Health checker for Redis connections."""

    def __init__(self, redis_client):
        self.redis_client = redis_client
        self._is_healthy = True
        self._last_check = 0
        self._check_interval = 30  # Check every 30 seconds.

    async def is_healthy(self) -> bool:
        """Check whether Redis is healthy."""
        import time

        current_time = time.time()

        # Return the cached result if the last check is still fresh.
        if current_time - self._last_check < self._check_interval:
            return self._is_healthy

        try:
            # Use PING to validate the connection.
            await self.redis_client.ping()
            self._is_healthy = True
            self._last_check = current_time
            return True
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            self._is_healthy = False
            self._last_check = current_time
            return False

    async def wait_for_healthy(self, timeout: float = 30.0) -> bool:
        """Wait for Redis to become healthy again."""
        import time

        start_time = time.time()

        while time.time() - start_time < timeout:
            if await self.is_healthy():
                return True
            await asyncio.sleep(1)

        return False
