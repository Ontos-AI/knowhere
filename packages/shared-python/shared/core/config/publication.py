"""Temporary global Publication strategy configuration.

The publication performance plan uses exactly one process-wide strategy
switch. It has two values and no per-request, per-namespace, or percentage
routing is permitted, so the value is validated as a closed enum at settings
construction time.
"""

from typing import Literal, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

PublicationStrategy = Literal["baseline", "candidate"]

PUBLICATION_STRATEGY_ENV_VAR: str = "KNOWHERE_PUBLICATION_STRATEGY"
PUBLICATION_STRATEGIES: tuple[PublicationStrategy, ...] = ("baseline", "candidate")
DEFAULT_PUBLICATION_STRATEGY: PublicationStrategy = "baseline"
DEFAULT_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES: int = 8 * 1024 * 1024


class PublicationStrategyError(ValueError):
    """Raised when a Publication strategy value is not supported."""


def normalize_publication_strategy(raw_value: object) -> PublicationStrategy:
    """Validate one raw strategy value and return its canonical form."""
    value = str(raw_value if raw_value is not None else "").strip().lower()
    if value not in PUBLICATION_STRATEGIES:
        raise PublicationStrategyError(
            f"{PUBLICATION_STRATEGY_ENV_VAR} must be one of "
            f"{', '.join(PUBLICATION_STRATEGIES)}; received {raw_value!r}"
        )
    return cast(PublicationStrategy, value)


class PublicationConfig(BaseSettings):
    """Publication strategy settings."""

    KNOWHERE_PUBLICATION_TRACE_ENABLED: bool = Field(
        default=False,
        description="Enable measurement-only publication traces for every attempt.",
    )

    KNOWHERE_PUBLICATION_STRATEGY: str = Field(
        default=DEFAULT_PUBLICATION_STRATEGY,
        description=(
            "Temporary global Publication strategy. Only 'baseline' and "
            "'candidate' are accepted; no request or namespace may override it."
        ),
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

    @field_validator("KNOWHERE_PUBLICATION_STRATEGY")
    @classmethod
    def validate_publication_strategy(cls, value: str) -> str:
        """Reject unsupported strategy values during settings construction."""
        return normalize_publication_strategy(value)
