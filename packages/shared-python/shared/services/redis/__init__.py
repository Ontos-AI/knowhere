"""Redis service exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .job_info_redis_service import JobInfoRedisService
    from .job_metadata_service import JobMetadataService
    from .key_builder import RedisKeyBuilder, RedisKeyType, redis_key_builder
    from .redis_alerts import AlertRule, RedisAlertManager, RedisAlertNotifier
    from .redis_monitor import RedisMonitor
    from .redis_service import RedisService
    from .redis_service_factory import RedisServiceFactory
    from .retry_policy import RedisHealthChecker, RedisRetry
    from .task_redis_service import TaskRedisService
    from .user_redis_service import UserRedisService

__all__ = [
    "RedisService",
    "RedisServiceFactory",
    "RedisMonitor",
    "RedisAlertManager",
    "RedisAlertNotifier",
    "AlertRule",
    "TaskRedisService",
    "UserRedisService",
    "JobInfoRedisService",
    "JobMetadataService",
    "RedisKeyBuilder",
    "RedisKeyType",
    "redis_key_builder",
    "RedisHealthChecker",
    "RedisRetry",
]

_EXPORT_MODULES: dict[str, str] = {
    "RedisService": "shared.services.redis.redis_service",
    "RedisServiceFactory": "shared.services.redis.redis_service_factory",
    "RedisMonitor": "shared.services.redis.redis_monitor",
    "RedisAlertManager": "shared.services.redis.redis_alerts",
    "RedisAlertNotifier": "shared.services.redis.redis_alerts",
    "AlertRule": "shared.services.redis.redis_alerts",
    "TaskRedisService": "shared.services.redis.task_redis_service",
    "UserRedisService": "shared.services.redis.user_redis_service",
    "JobInfoRedisService": "shared.services.redis.job_info_redis_service",
    "JobMetadataService": "shared.services.redis.job_metadata_service",
    "RedisKeyBuilder": "shared.services.redis.key_builder",
    "RedisKeyType": "shared.services.redis.key_builder",
    "redis_key_builder": "shared.services.redis.key_builder",
    "RedisHealthChecker": "shared.services.redis.retry_policy",
    "RedisRetry": "shared.services.redis.retry_policy",
}


def __getattr__(name: str) -> Any:
    """Load Redis package exports on first access."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return public Redis package exports."""
    return sorted([*globals(), *__all__])
