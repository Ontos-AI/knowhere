"""
MinerU service configuration
"""

from typing import Literal

from pydantic import BaseModel, Field


class MineruConfig(BaseModel):
    """MinerU PDF parsing service configuration"""

    MINERU_URL: str = Field(
        default="https://mineru.net/api/v4",
        description="Base MinerU API URL without endpoint path.",
    )
    MINERU_API_KEYS: str = Field(
        default="",
        description="MinerU API key pool. Supports JSON array or comma/newline-separated values; entries may use token_id=api_key format.",
    )
    MINERU_TOKEN_RPM_LIMIT: int = Field(
        default=300,
        description="Per-token requests-per-minute limit.",
    )
    MINERU_TOKEN_DAILY_LIMIT: int = Field(
        default=10000,
        description="Per-token daily request limit.",
    )
    MINERU_TOKEN_COOLDOWN_SECONDS: int = Field(
        default=60,
        description="Cooldown seconds after a token is rate-limited.",
    )
    MINERU_API_TIMEOUT: int = Field(
        default=60,
        description="Timeout in seconds for MinerU API calls.",
    )
    MINERU_UPLOAD_CONNECT_TIMEOUT: int = Field(
        default=10,
        description="Connect timeout in seconds for file uploads.",
    )
    MINERU_UPLOAD_READ_TIMEOUT: int = Field(
        default=600,
        description="Read timeout in seconds for file uploads.",
    )
    MINERU_RATE_LIMIT_MAX_RETRY_AFTER: int = Field(
        default=60,
        description="Max retry-after seconds for rate-limited requests.",
    )
    MINERU_POOL_MAXSIZE: int = Field(
        default=50,
        description="Max pooled connections per host for MinerU requests.",
    )
    MINERU_UPLOAD_RETRY_TOTAL: int = Field(
        default=3,
        description="Number of retries for transient upload failures.",
    )
    MINERU_UPLOAD_RETRY_BACKOFF_FACTOR: int = Field(
        default=2,
        description="Backoff factor between upload retries.",
    )
    MINERU_UPLOAD_MODE_ENABLED: bool = Field(
        default=False,
        description=(
            "Use MinerU direct-upload ingestion instead of S3 URL mode. "
            "Disabled by default so workers prefer S3 URL mode when a reusable "
            "source object is available."
        ),
    )
    MINERU_URL_MODE_PRESIGN_EXPIRY: int = Field(
        default=3600,
        description="Presigned URL TTL in seconds for S3 URL mode ingestion.",
    )
    MINERU_LOCAL_PROJECT_PATH: str = Field(
        default="",
        description="Absolute path to a separate local MinerU checkout.",
    )
    MINERU_LOCAL_UV_EXECUTABLE: str = Field(
        default="uv",
        description="Absolute path or PATH-resolvable uv executable for local MinerU.",
    )
    MINERU_LOCAL_TIMEOUT_SECONDS: int = Field(
        default=1800,
        gt=0,
        description="Timeout for one isolated local MinerU parse.",
    )
    MINERU_LOCAL_BACKEND: str = Field(
        default="pipeline",
        description="Default local MinerU backend for standalone exports.",
    )
    MINERU_LOCAL_METHOD: str = Field(
        default="auto",
        description="Default local MinerU parsing method.",
    )
    MINERU_LOCAL_LANGUAGE: str = Field(
        default="en",
        description="Default local MinerU document language.",
    )
    MINERU_LOCAL_OFFLINE: bool = Field(
        default=True,
        description="Request local-only model loading for standalone exports.",
    )
    MINERU_LOCAL_MAX_LOG_CHARS: int = Field(
        default=8000,
        gt=0,
        description="Maximum sanitized stderr characters attached to local errors.",
    )
    MINERU_PROVIDER: Literal["cloud", "local"] = Field(
        default="cloud",
        description="PDF MinerU provider; local is explicit opt-in with no cloud fallback.",
    )
    MINERU_LOCAL_SHARD_CONCURRENCY: int = Field(
        default=1,
        ge=1,
        description="Maximum concurrent local MinerU PDF shard parses.",
    )
