"""Cross-instance concurrency control for database publication."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.services.redis.redis_sync_service import SyncRedisService

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisPublicationSemaphore:
    """Lease-based Redis semaphore using one key per permit."""

    def __init__(
        self,
        redis_service: RedisService,
        *,
        concurrency: int,
        lease_seconds: int,
        acquire_timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._redis = redis_service
        self._concurrency = concurrency
        self._lease_seconds = lease_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._owner = uuid.uuid4().hex
        self._permit_key: str | None = None

    async def acquire(self) -> float:
        """Acquire a permit and return the time spent waiting in seconds."""
        started_at = time.perf_counter()
        deadline = started_at + self._acquire_timeout_seconds
        while time.perf_counter() < deadline:
            for permit_number in range(self._concurrency):
                key = f"lock:materialization_publication:{permit_number}"
                acquired = await self._redis.set_nx(
                    key,
                    self._owner,
                    ex=self._lease_seconds,
                )
                if acquired:
                    self._permit_key = key
                    return time.perf_counter() - started_at
            await asyncio.sleep(self._poll_interval_seconds)
        raise TimeoutError("Timed out waiting for a materialization publication permit")

    async def release(self) -> bool:
        """Release this semaphore's permit only when still its owner."""
        if self._permit_key is None:
            return False
        key = self._permit_key
        self._permit_key = None
        try:
            result = await self._redis.eval(
                _RELEASE_SCRIPT,
                keys=[key],
                args=[self._owner],
            )
            return bool(result)
        except Exception as error:
            logger.warning(f"Failed to release publication permit: {error}")
            return False

    async def __aenter__(self) -> "RedisPublicationSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.release()


class SyncRedisPublicationSemaphore:
    """Synchronous lease-based semaphore for gevent worker publication."""

    def __init__(
        self,
        redis_service: SyncRedisService,
        *,
        concurrency: int,
        lease_seconds: int,
        acquire_timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._redis = redis_service
        self._concurrency = concurrency
        self._lease_seconds = lease_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._owner = uuid.uuid4().hex
        self._permit_key: str | None = None

    def acquire(self) -> float:
        """Acquire a permit and return the time spent waiting in seconds."""
        started_at = time.perf_counter()
        deadline = started_at + self._acquire_timeout_seconds
        while time.perf_counter() < deadline:
            for permit_number in range(self._concurrency):
                key = f"lock:materialization_publication:{permit_number}"
                if self._redis.set_nx(key, self._owner, ex=self._lease_seconds):
                    self._permit_key = key
                    return time.perf_counter() - started_at
            time.sleep(self._poll_interval_seconds)
        raise TimeoutError("Timed out waiting for a materialization publication permit")

    def release(self) -> bool:
        """Release this semaphore's permit only when still its owner."""
        if self._permit_key is None:
            return False
        key = self._permit_key
        self._permit_key = None
        try:
            return bool(
                self._redis.eval(_RELEASE_SCRIPT, keys=[key], args=[self._owner])
            )
        except Exception as error:
            logger.warning(f"Failed to release publication permit: {error}")
            return False

    def __enter__(self) -> "SyncRedisPublicationSemaphore":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()
