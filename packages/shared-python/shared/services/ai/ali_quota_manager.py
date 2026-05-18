"""
AliQuotaManager — Redis-backed token pool for Aliyun DashScope API keys.

Distributes requests across multiple ALI_API_KEYS using the shared
BaseQuotaManager, with per-token RPM / daily quotas and 429 cooldown.
"""

from __future__ import annotations

from typing import List, Optional

from shared.core.config import settings
from shared.services.redis.redis_sync_service import (
    SyncRedisService,
    SyncRedisServiceFactory,
)
from shared.services.ai.quota_manager import BaseQuotaManager, TokenConfig


class AliQuotaManager(BaseQuotaManager):
    """Quota-aware Ali DashScope token manager backed by Redis."""

    SERVICE_PREFIX = "ali"
    CURSOR_KEY = "ali:quota:cursor"
    user_message = "AI service is busy right now. Please retry shortly."

    @classmethod
    def from_settings(
        cls, redis_service: Optional[SyncRedisService] = None
    ) -> "AliQuotaManager":
        """Create an AliQuotaManager from application settings."""
        tokens = cls.parse_tokens_from_settings()
        instance = cls(redis_service or SyncRedisServiceFactory.get_service(), tokens)
        instance.default_cooldown_seconds = settings.ALI_TOKEN_COOLDOWN_SECONDS
        return instance

    @staticmethod
    def parse_tokens_from_settings() -> List[TokenConfig]:
        default_rpm_limit: int = settings.ALI_TOKEN_RPM_LIMIT
        default_daily_limit: int = settings.ALI_TOKEN_DAILY_LIMIT
        raw_pool: str = (settings.ALI_API_KEYS or "").strip()

        if raw_pool:
            parsed = BaseQuotaManager.parse_token_specs(
                raw_pool,
                default_rpm_limit=default_rpm_limit,
                default_daily_limit=default_daily_limit,
            )
            if parsed:
                return parsed

        raise ValueError(
            "No Ali API keys configured. Set ALI_API_KEYS to a comma/newline-separated list or JSON array."
        )


_ali_quota_manager: Optional[AliQuotaManager] = None


def get_ali_quota_manager() -> AliQuotaManager:
    """Return a singleton AliQuotaManager for worker processes."""
    global _ali_quota_manager
    if _ali_quota_manager is None:
        _ali_quota_manager = AliQuotaManager.from_settings()
    return _ali_quota_manager
