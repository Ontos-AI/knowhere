"""
ILoveApiQuotaManager — Redis-backed project pool for iLoveAPI keys.

Each iLoveAPI public_key+secret_key pair represents a separate project
with its own credit quota (free tier: 2,500 credits/month, officepdf
costs 10 credits/file = ~250 files/month).

Distributes requests across multiple projects using the shared
BaseQuotaManager, with RPM burst protection, daily file limits,
429 cooldown, and a global in-flight concurrency limiter to prevent
worker pool starvation when iLoveAPI is slow or rate-limited.

This is an optional enhancement path — all failures fail open to
LibreOffice local conversion.
"""

from __future__ import annotations

import json
from typing import List, Optional

from loguru import logger

from shared.core.config import settings
from shared.services.redis.redis_sync_service import (
    SyncRedisService,
    SyncRedisServiceFactory,
)
from shared.services.quota.token_pool import BaseQuotaManager, TokenConfig


class ILoveApiQuotaManager(BaseQuotaManager):
    """Quota-aware iLoveAPI token manager backed by Redis."""

    SERVICE_PREFIX = "iloveapi"
    CURSOR_KEY = "iloveapi:quota:cursor"
    user_message = "Document parsing service is busy right now. Please retry shortly."

    INFLIGHT_KEY = "iloveapi:inflight"
    INFLIGHT_TTL_SECONDS = 300  # 5-minute safety net; auto-expires stale counts

    _ACQUIRE_INFLIGHT_SCRIPT: str = """
local key = KEYS[1]
local max_concurrent = tonumber(ARGV[1])
local ttl_seconds = tonumber(ARGV[2])

local current = tonumber(redis.call('GET', key) or '0')
if current >= max_concurrent then
  return {0, current}
end

local new_count = redis.call('INCR', key)
-- Set TTL only if not already set (safety net for crash recovery)
if redis.call('TTL', key) < 0 then
  redis.call('EXPIRE', key, ttl_seconds)
end

return {1, new_count}
"""

    _RELEASE_INFLIGHT_SCRIPT: str = """
local key = KEYS[1]
local current = tonumber(redis.call('GET', key) or '0')
if current <= 0 then
  redis.call('DEL', key)
  return 0
end
return redis.call('DECR', key)
"""

    def __init__(
        self,
        redis_service: SyncRedisService,
        tokens: List[TokenConfig],
        max_concurrent: int = 5,
    ) -> None:
        super().__init__(redis_service, tokens)
        self.max_concurrent = max_concurrent

    @classmethod
    def from_settings(
        cls, redis_service: Optional[SyncRedisService] = None
    ) -> "ILoveApiQuotaManager":
        """Create an ILoveApiQuotaManager from application settings."""
        tokens = cls.parse_tokens_from_settings()
        max_concurrent: int = getattr(settings, "ILOVEAPI_MAX_CONCURRENT", 5)
        instance = cls(
            redis_service or SyncRedisServiceFactory.get_service(),
            tokens,
            max_concurrent=max_concurrent,
        )
        instance.default_cooldown_seconds = 60
        return instance

    @staticmethod
    def parse_tokens_from_settings() -> List[TokenConfig]:
        default_rpm_limit: int = getattr(settings, "ILOVEAPI_TOKEN_RPM_LIMIT", 25)
        default_daily_limit: int = getattr(settings, "ILOVEAPI_TOKEN_DAILY_LIMIT", 250)
        raw_pool: str = (getattr(settings, "ILOVEAPI_KEYS", "") or "").strip()

        legacy_pub: str = (getattr(settings, "ILOVEAPI_PUBLIC_KEY", "") or "").strip()
        legacy_sec: str = (getattr(settings, "ILOVEAPI_SECRET_KEY", "") or "").strip()

        specs: List[TokenConfig] = []
        if raw_pool:
            try:
                loaded = json.loads(raw_pool)
                if isinstance(loaded, list):
                    for index, entry in enumerate(loaded):
                        pub = str(entry.get("public_key") or "").strip()
                        sec = str(entry.get("secret_key") or "").strip()
                        if pub and sec:
                            specs.append(
                                TokenConfig(
                                    token_id=str(
                                        entry.get("token_id") or f"iloveapi-{index + 1}"
                                    ),
                                    api_key=f"{pub}:{sec}",
                                    rpm_limit=int(
                                        entry.get("rpm_limit") or default_rpm_limit
                                    ),
                                    daily_limit=int(
                                        entry.get("daily_limit") or default_daily_limit
                                    ),
                                )
                            )
            except json.JSONDecodeError as exc:
                logger.debug(f"Invalid I Love API token JSON ignored: {exc}")

        if not specs and legacy_pub and legacy_sec:
            specs.append(
                TokenConfig(
                    token_id="iloveapi-default",
                    api_key=f"{legacy_pub}:{legacy_sec}",
                    rpm_limit=default_rpm_limit,
                    daily_limit=default_daily_limit,
                )
            )

        if not specs:
            raise ValueError(
                "No iLoveAPI keys configured. Set ILOVEAPI_KEYS to a JSON array containing public_key and secret_key."
            )

        return specs

    # ------------------------------------------------------------------
    # In-flight concurrency limiter
    # ------------------------------------------------------------------

    def acquire_inflight(self) -> Optional[bool]:
        """Try to acquire an in-flight slot.

        Returns:
            True: reserved a Redis-backed slot and must release it later.
            False: at capacity; caller should fail open to local conversion.
            None: Redis failed; caller may proceed, but must not release.
        """
        try:
            result = self.redis.eval(
                self._ACQUIRE_INFLIGHT_SCRIPT,
                keys=[self.INFLIGHT_KEY],
                args=[self.max_concurrent, self.INFLIGHT_TTL_SECONDS],
            )
            if isinstance(result, list) and len(result) >= 2:
                acquired = int(result[0]) == 1
                current_count = int(result[1])
                if not acquired:
                    logger.bind(
                        service=self.SERVICE_PREFIX,
                        step="concurrency_exceeded",
                        current_inflight=current_count,
                        max_concurrent=self.max_concurrent,
                    ).warning(
                        f"iLoveAPI concurrency limit reached ({current_count}/{self.max_concurrent})"
                    )
                return acquired
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to check iLoveAPI in-flight counter; failing open to allow request"
            )
        # Fail open on Redis errors — allow the request through without a lease.
        return None

    def release_inflight(self) -> None:
        """Release an in-flight slot after conversion completes (success or failure)."""
        try:
            self.redis.eval(
                self._RELEASE_INFLIGHT_SCRIPT,
                keys=[self.INFLIGHT_KEY],
                args=[],
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to release iLoveAPI in-flight slot"
            )

    def get_inflight_count(self) -> int:
        """Return current in-flight count (for observability / testing)."""
        try:
            raw = self.redis.get(self.INFLIGHT_KEY, 0)
            return max(0, int(raw or 0))
        except Exception:
            return 0


_iloveapi_quota_manager: Optional[ILoveApiQuotaManager] = None


def get_iloveapi_quota_manager() -> ILoveApiQuotaManager:
    """Return a singleton ILoveApiQuotaManager for worker processes."""
    global _iloveapi_quota_manager
    if _iloveapi_quota_manager is None:
        _iloveapi_quota_manager = ILoveApiQuotaManager.from_settings()
    return _iloveapi_quota_manager
