"""Lexical backend selection, publication measurement, and snapshot bounds."""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES: int = 8 * 1024 * 1024


class PublicationConfig(BaseSettings):
    """Explicit backend rollout gate and publication support settings."""

    KNOWHERE_LEXICAL_BACKEND: str = Field(default="postgresql", pattern="^(postgresql|opensearch)$")
    KNOWHERE_OPENSEARCH_URL: str = Field(default="http://127.0.0.1:9200")
    KNOWHERE_OPENSEARCH_CA_FILE: str | None = None
    KNOWHERE_OPENSEARCH_CLIENT_CERT_FILE: str | None = None
    KNOWHERE_OPENSEARCH_CLIENT_KEY_FILE: str | None = None
    KNOWHERE_OPENSEARCH_USERNAME: str | None = None
    KNOWHERE_OPENSEARCH_PASSWORD_FILE: str | None = None
    KNOWHERE_OPENSEARCH_INDEX_PREFIX: str = Field(default="knowhere-lexical-v1", pattern="^[a-z][a-z0-9-]{0,39}$")
    KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES: int = Field(
        default=4 * 1024 * 1024, ge=1, le=32 * 1024 * 1024,
        description="Maximum UTF-8 NDJSON bytes per lexical bulk request.",
    )
    KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK: int = Field(default=360, ge=1, le=400)
    KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK: int = Field(default=320, ge=0, lt=400)
    KNOWHERE_INDEXING_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=300)
    KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS: float = Field(default=300, gt=0, le=3600)
    KNOWHERE_INDEXING_UNSTARTED_TIMEOUT_SECONDS: int = Field(default=300, ge=1, le=1800)
    KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS: float = Field(default=2, gt=0, le=10)
    KNOWHERE_INDEXING_MAXIMUM_UNIT_CHARACTERS: int = Field(
        default=524_288, ge=1_024, le=1_048_576,
        description="Maximum raw character/work count per record or scoring unit, before tokenization.",
    )
    KNOWHERE_INDEXING_MAXIMUM_CJK_SPAN_CHARACTERS: int = Field(default=4_096, ge=256, le=8_192)
    KNOWHERE_INDEXING_MAXIMUM_HIERARCHY_DEPTH: int = Field(default=256, ge=1, le=1_024)
    KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)

    KNOWHERE_PUBLICATION_TRACE_ENABLED: bool = Field(
        default=False,
        description="Enable measurement-only publication traces for every attempt.",
    )

    KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES: int = Field(
        default=DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES,
        ge=0,
        description=(
            "Maximum compressed namespace snapshot size maintained during "
            "publication. Larger snapshots become stale best-effort cache rows "
            "and retrieval uses the manifest/table fallback. Zero disables the "
            "bound."
        ),
    )

    @model_validator(mode="after")
    def validateIdleWatermarks(self) -> Self:
        if self.KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK >= self.KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK:
            raise ValueError("OpenSearch idle low watermark must be below the high watermark")
        return self
