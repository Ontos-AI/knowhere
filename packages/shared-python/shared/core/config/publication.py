"""Publication measurement and snapshot size settings."""

from pydantic import Field
from pydantic_settings import BaseSettings

DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES: int = 8 * 1024 * 1024


class PublicationConfig(BaseSettings):
    """Measurement-only and bounded snapshot settings."""

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
