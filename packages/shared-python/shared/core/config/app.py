"""Application configuration — assembles all config components."""

from typing import Callable, cast

from pydantic_settings import SettingsConfigDict

from .ai import AIConfig
from .base import BaseConfig
from .billing import BillingConfig
from .celery import CeleryConfig
from .database import DatabaseConfig
from .job import JobConfig
from .mineru import MineruConfig
from .qstash import QStashConfig
from .redis import RedisConfig, RedisConfigManager, RedisPoolManager
from .retrieval import RetrievalConfig
from .storage import StorageConfig


class AppConfig(
    BaseConfig,
    DatabaseConfig,
    RedisConfig,
    CeleryConfig,
    QStashConfig,
    StorageConfig,
    AIConfig,
    MineruConfig,
    BillingConfig,
    JobConfig,
    RetrievalConfig,
):
    """Application configuration — all config components merged."""

    def validate_all(self) -> bool:
        """Validate the combined application configuration."""
        validations = [
            self.validate_file_paths(),
            self.validate_database_config(),
            self.validate_redis_config(),
            self.validate_billing_config(),
        ]

        return all(validations)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Create the shared application config instance.
app_config = cast(Callable[[], AppConfig], AppConfig)()

# Backward-compatible alias.
settings = app_config

# Create the Redis connection-pool manager.
redis_pool_manager = RedisPoolManager(app_config)

# Create the Redis config manager.
redis_config_manager = RedisConfigManager(app_config)
