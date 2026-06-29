"""Base application configuration."""

import os

from loguru import logger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_TELEMETRY_POSTHOG_PROJECT_KEY = (
    "phc_nWXdQnhvJFcjcvVNjQ8J8LhDYa9uvHfYhiuovf4Fzq64"
)


class BaseConfig(BaseSettings):
    """Base application configuration."""

    # Environment configuration.
    ENVIRONMENT: str = Field(default="production", description="Runtime environment")
    APP_ENV: str = Field(
        default="",
        description="Deploy environment (<empty>|development|staging|production)",
    )
    LOG_LEVEL: str = Field(default="INFO", description="Log level")

    # Application metadata.
    APP_TITLE: str = Field(default="Knowhere API", description="Application title")
    APP_VERSION: str = Field(
        default_factory=lambda: os.getenv("APP_VERSION", "1.0.0"),
        description="Application version read from the APP_VERSION environment variable",
    )
    APP_DESCRIPTION: str = Field(
        default="AI-powered document parsing and retrieval backend",
        description="Application description",
    )

    # Logging configuration
    LOGFIRE_TOKEN: str = Field(
        default="", description="Logfire API token for distributed tracing"
    )
    TELEMETRY_ENABLED: bool = Field(
        default=True,
        description="Enable anonymous product telemetry for self-hosted deployments",
    )
    TELEMETRY_POSTHOG_HOST: str = Field(
        default="https://us.i.posthog.com",
        description="PostHog ingestion host for anonymous telemetry events",
    )
    TELEMETRY_POSTHOG_PROJECT_KEY: str = Field(
        default_factory=lambda: os.getenv(
            "NEXT_PUBLIC_POSTHOG_KEY",
            DEFAULT_TELEMETRY_POSTHOG_PROJECT_KEY,
        ),
        description="PostHog project token for anonymous telemetry events",
    )
    TELEMETRY_INSTALLATION_ID: str = Field(
        default="",
        description="Optional operator-provided anonymous self-hosted installation id",
    )
    TELEMETRY_INSTALLATION_ID_PATH: str = Field(
        default="/data/secrets/telemetry-installation-id",
        description="Persistent file used to store the generated telemetry installation id",
    )
    TELEMETRY_BATCH_SIZE: int = Field(
        default=20,
        description="Maximum anonymous telemetry events per PostHog batch request",
    )
    TELEMETRY_REQUEST_TIMEOUT_SECONDS: float = Field(
        default=2.0,
        description="Short timeout for outbound anonymous telemetry requests",
    )
    TELEMETRY_DEPLOYMENT_MODE: str = Field(
        default="self_hosted",
        description="Deployment-mode label attached to anonymous telemetry events",
    )
    TELEMETRY_AGGREGATE_INTERVAL_SECONDS: int = Field(
        default=300,
        description="Interval for anonymous self-hosted aggregate telemetry snapshots",
    )

    # Security configuration.
    WEBHOOK_MASTER_KEY: str = Field(
        default="", description="Webhook encryption master key"
    )
    INTERNAL_DASHBOARD_ENDPOINT: str = Field(
        default="http://localhost:3000", description="Internal Dashboard endpoint"
    )
    API_STANDALONE_MODE_ENABLED: bool = Field(
        default=False,
        description=(
            "API-only mode that creates a minimal Better Auth-compatible user table "
            "before Alembic migrations when the dashboard is absent"
        ),
    )

    # Local path configuration.
    TMP_PATH: str = Field(..., description="Temporary-file path")

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v):
        """Validate the runtime environment."""
        if v not in ["development", "staging", "production"]:
            raise ValueError("ENVIRONMENT must be development, staging, or production")
        return v

    @field_validator("APP_ENV")
    @classmethod
    def validate_app_env(cls, v):
        app_env = v.strip().lower()
        if app_env and app_env not in ["development", "staging", "production"]:
            raise ValueError("APP_ENV must be empty, staging, or production")
        return app_env

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v):
        """Validate the log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"LOG_LEVEL must be one of {valid_levels}")
        return v.upper()

    @field_validator("TELEMETRY_POSTHOG_HOST")
    @classmethod
    def validate_telemetry_posthog_host(cls, v):
        """Normalize the PostHog ingestion host."""
        return v.strip().rstrip("/")

    @field_validator("TELEMETRY_BATCH_SIZE")
    @classmethod
    def validate_telemetry_batch_size(cls, v):
        """Validate anonymous telemetry batch size."""
        if v < 1:
            raise ValueError("TELEMETRY_BATCH_SIZE must be at least 1")
        return v

    @field_validator("TELEMETRY_REQUEST_TIMEOUT_SECONDS")
    @classmethod
    def validate_telemetry_request_timeout_seconds(cls, v):
        """Validate anonymous telemetry request timeout."""
        if v <= 0:
            raise ValueError("TELEMETRY_REQUEST_TIMEOUT_SECONDS must be positive")
        return v

    @field_validator("TELEMETRY_AGGREGATE_INTERVAL_SECONDS")
    @classmethod
    def validate_telemetry_aggregate_interval_seconds(cls, v):
        """Validate anonymous aggregate telemetry interval."""
        if v < 60:
            raise ValueError("TELEMETRY_AGGREGATE_INTERVAL_SECONDS must be at least 60")
        return v

    def validate_file_paths(self) -> bool:
        """Validate required local file paths."""
        paths_to_check = {
            "TMP_PATH": self.TMP_PATH,
        }

        for name, path in paths_to_check.items():
            if not os.path.exists(path):
                logger.warning(f"Path does not exist: {name} = {path}")
                return False

        logger.info("File-path validation succeeded")
        return True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unrelated environment variables.
    )
