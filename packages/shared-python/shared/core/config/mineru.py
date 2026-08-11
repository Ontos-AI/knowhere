"""
MinerU service configuration
"""

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
    MINERU_LOCAL_MODE: bool = Field(
        default=False,
        description=(
            "Use a local (self-hosted) MinerU instance via the synchronous "
            "/file_parse endpoint instead of the cloud batch APIs. When true, "
            "MINERU_URL should point at the local MinerU base URL "
            "(e.g. http://host.docker.internal:8000)."
        ),
    )
    MINERU_LOCAL_LANG_LIST: str = Field(
        default="ch",
        description=(
            "Language code passed to local MinerU's /file_parse lang_list "
            "parameter. Local MinerU does not accept 'auto'; the default 'ch' "
            "covers Chinese, English, Japanese, Traditional Chinese, and Latin."
        ),
    )
    MINERU_LOCAL_BACKEND: str = Field(
        default="pipeline",
        description=(
            "Backend passed to local MinerU's /file_parse backend parameter. "
            "Use 'pipeline' for CPU-only parsing or 'vlm-engine' for GPU VLM."
        ),
    )
    MINERU_LOCAL_TIMEOUT: int = Field(
        default=3600,
        description=(
            "Per-shard timeout in seconds for local MinerU's synchronous "
            "/file_parse call. Includes queue wait when "
            "MINERU_SHARD_CONCURRENCY > 1, because local MinerU is "
            "single-concurrency by default."
        ),
    )
