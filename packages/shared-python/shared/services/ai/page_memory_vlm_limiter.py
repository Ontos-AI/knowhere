"""Redis-backed global in-flight gate for page-memory VLM calls."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.redis.redis_sync_service import (
    SyncRedisService,
    SyncRedisServiceFactory,
)


@dataclass(frozen=True)
class PageMemoryVlmLease:
    """A reserved page-memory VLM capacity slot."""

    usage_task: str
    acquired_at: float
    current_inflight: int


class PageMemoryVlmLimiter:
    """Global Redis counter for page-memory VLM in-flight capacity."""

    INFLIGHT_KEY = "page_memory:vlm:inflight"
    user_message = "Document parsing VLM capacity is busy. Please retry shortly."

    _ACQUIRE_SCRIPT: str = """
local key = KEYS[1]
local max_inflight = tonumber(ARGV[1])
local ttl_seconds = tonumber(ARGV[2])

local current = tonumber(redis.call('GET', key) or '0')
if current >= max_inflight then
  redis.call('EXPIRE', key, ttl_seconds)
  return {0, current}
end

local new_count = redis.call('INCR', key)
redis.call('EXPIRE', key, ttl_seconds)
return {1, new_count}
"""

    _RELEASE_SCRIPT: str = """
local key = KEYS[1]
local current = tonumber(redis.call('GET', key) or '0')
if current <= 1 then
  redis.call('DEL', key)
  return 0
end
return redis.call('DECR', key)
"""

    def __init__(
        self,
        redis_service: SyncRedisService,
        *,
        max_inflight: int,
        lease_ttl_seconds: int,
        wait_timeout_seconds: int,
    ) -> None:
        self.redis = redis_service
        self.max_inflight = max(1, max_inflight)
        self.lease_ttl_seconds = max(1, lease_ttl_seconds)
        self.wait_timeout_seconds = max(1, wait_timeout_seconds)

    @classmethod
    def from_settings(
        cls,
        redis_service: Optional[SyncRedisService] = None,
    ) -> "PageMemoryVlmLimiter":
        """Create a limiter from internal page-memory settings."""
        return cls(
            redis_service or SyncRedisServiceFactory.get_service(),
            max_inflight=int(getattr(settings, "PAGE_MEMORY_VLM_MAX_INFLIGHT", 16)),
            lease_ttl_seconds=int(
                getattr(settings, "PAGE_MEMORY_VLM_LEASE_TTL_SECONDS", 600)
            ),
            wait_timeout_seconds=int(
                getattr(settings, "PAGE_MEMORY_VLM_WAIT_TIMEOUT_SECONDS", 120)
            ),
        )

    def acquire(self, *, usage_task: str) -> PageMemoryVlmLease:
        """Wait for and reserve one global VLM capacity slot."""
        started_at = time.monotonic()
        deadline = started_at + self.wait_timeout_seconds
        last_inflight = 0

        while time.monotonic() < deadline:
            acquired, current_inflight = self._try_acquire(usage_task=usage_task)
            last_inflight = current_inflight
            if acquired:
                wait_ms = int((time.monotonic() - started_at) * 1000)
                logger.bind(
                    event="page_memory.vlm_gate.acquired",
                    usage_task=usage_task,
                    current_inflight=current_inflight,
                    max_inflight=self.max_inflight,
                    wait_ms=wait_ms,
                ).info("page_memory.vlm_gate.acquired")
                return PageMemoryVlmLease(
                    usage_task=usage_task,
                    acquired_at=time.monotonic(),
                    current_inflight=current_inflight,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self._jittered_backoff(last_inflight), remaining))

        raise UnavailableException(
            internal_message=(
                "Page-memory VLM capacity gate timed out "
                f"after {self.wait_timeout_seconds}s "
                f"({last_inflight}/{self.max_inflight} in-flight)"
            ),
            retry_after=max(1, min(self.wait_timeout_seconds, 60)),
            limit=self.max_inflight,
            period="minute",
            user_message=self.user_message,
        )

    def release(self, lease: PageMemoryVlmLease) -> None:
        """Release a previously acquired page-memory VLM capacity slot."""
        try:
            self.redis.eval(
                self._RELEASE_SCRIPT,
                keys=[self.INFLIGHT_KEY],
                args=[],
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to release page-memory VLM in-flight slot for {}",
                lease.usage_task,
            )

    def get_inflight_count(self) -> int:
        """Return the current Redis in-flight count."""
        try:
            raw = self.redis.get(self.INFLIGHT_KEY, 0)
            return max(0, int(raw or 0))
        except Exception:
            return 0

    def _try_acquire(self, *, usage_task: str) -> tuple[bool, int]:
        try:
            result = self.redis.eval(
                self._ACQUIRE_SCRIPT,
                keys=[self.INFLIGHT_KEY],
                args=[self.max_inflight, self.lease_ttl_seconds],
            )
        except Exception as exc:
            logger.opt(exception=True).warning(
                "Failed to check page-memory VLM in-flight counter for {}",
                usage_task,
            )
            raise UnavailableException(
                internal_message="Failed to check page-memory VLM capacity gate",
                retry_after=5,
                user_message=self.user_message,
                original_exception=exc,
            ) from exc

        if not isinstance(result, list) or len(result) < 2:
            raise UnavailableException(
                internal_message=f"Unexpected page-memory VLM gate result: {result!r}",
                retry_after=5,
                user_message=self.user_message,
            )
        return int(result[0]) == 1, max(0, int(result[1]))

    def _jittered_backoff(self, current_inflight: int) -> float:
        pressure = max(current_inflight - self.max_inflight + 1, 1)
        base = min(0.25 * pressure, 2.0)
        return base + random.uniform(0.0, 0.2)

    def _sleep(self, seconds: float) -> None:
        try:
            import gevent

            gevent.sleep(seconds)
        except ImportError:
            time.sleep(seconds)


_page_memory_vlm_limiter: Optional[PageMemoryVlmLimiter] = None


def get_page_memory_vlm_limiter() -> PageMemoryVlmLimiter:
    """Return a singleton page-memory VLM limiter for worker processes."""
    global _page_memory_vlm_limiter
    if _page_memory_vlm_limiter is None:
        _page_memory_vlm_limiter = PageMemoryVlmLimiter.from_settings()
    return _page_memory_vlm_limiter
