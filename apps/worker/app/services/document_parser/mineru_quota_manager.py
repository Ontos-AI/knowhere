"""
Quota-aware MinerU token selection for sync worker flows.

Inherits from BaseQuotaManager for the shared Redis-backed quota logic.
"""

from __future__ import annotations

from typing import List, Optional


from shared.core.config import settings
from shared.services.redis.redis_sync_service import (
    SyncRedisService,
    SyncRedisServiceFactory,
)
from shared.services.quota.token_pool import BaseQuotaManager, TokenConfig, TokenLease

# Backward-compatible aliases so existing imports keep working
MinerUTokenConfig = TokenConfig
MinerUTokenLease = TokenLease


class MinerUQuotaManager(BaseQuotaManager):
    """Quota-aware MinerU token manager backed by Redis."""

    SERVICE_PREFIX = "mineru"
    CURSOR_KEY = "mineru:quota:cursor"
    user_message = "Document processing is busy right now. Please retry shortly."

    @classmethod
    def from_settings(
        cls, redis_service: Optional[SyncRedisService] = None
    ) -> "MinerUQuotaManager":
        tokens = cls._parse_tokens_from_settings()
        instance = cls(redis_service or SyncRedisServiceFactory.get_service(), tokens)
        instance.default_cooldown_seconds = settings.MINERU_TOKEN_COOLDOWN_SECONDS
        return instance

    @staticmethod
    def _parse_tokens_from_settings() -> List[TokenConfig]:
        default_rpm_limit: int = settings.MINERU_TOKEN_RPM_LIMIT
        default_daily_limit: int = settings.MINERU_TOKEN_DAILY_LIMIT
        raw_pool: str = (settings.MINERU_API_KEYS or "").strip()

        if raw_pool:
            parsed = BaseQuotaManager.parse_token_specs(
                raw_pool,
                default_rpm_limit=default_rpm_limit,
                default_daily_limit=default_daily_limit,
            )
            if parsed:
                return parsed

        raise ValueError("No MinerU API keys configured")


_quota_manager: Optional[MinerUQuotaManager] = None


def get_mineru_quota_manager() -> MinerUQuotaManager:
    """Return a singleton quota manager for worker processes."""
    global _quota_manager
    if _quota_manager is None:
        _quota_manager = MinerUQuotaManager.from_settings()
    return _quota_manager
