"""Compatibility wrapper for the iLoveAPI quota manager."""

from shared.services.ai.iloveapi_quota_manager import (
    ILoveApiQuotaManager,
    get_iloveapi_quota_manager,
)

__all__ = [
    "ILoveApiQuotaManager",
    "get_iloveapi_quota_manager",
]
