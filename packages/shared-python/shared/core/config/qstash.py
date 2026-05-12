"""
QStash configuration for outbound webhook delivery via Upstash QStash.
"""

from typing import Optional

from pydantic import BaseModel, Field


class QStashConfig(BaseModel):
    """QStash configuration for webhook delivery."""

    # QStash API credentials (from Upstash console)
    QSTASH_TOKEN: Optional[str] = Field(default=None, description="QStash API token")
    QSTASH_BASE_URL: Optional[str] = Field(
        default=None,
        description="QStash API base URL override for local development and tests",
    )
    QSTASH_CURRENT_SIGNING_KEY: Optional[str] = Field(
        default=None, description="QStash current signing key for callback verification"
    )
    QSTASH_NEXT_SIGNING_KEY: Optional[str] = Field(
        default=None, description="QStash next signing key for zero-downtime rotation"
    )

    # Public base URL that QStash callbacks are sent to
    QSTASH_CALLBACK_BASE_URL: Optional[str] = Field(
        default=None, description="Public API base URL for QStash callback endpoints"
    )

    # QStash retry configuration (approximate exponential backoff)
    QSTASH_MAX_RETRIES: int = Field(
        default=5, description="QStash max delivery retries"
    )

    @property
    def qstash_callback_url(self) -> Optional[str]:
        """Build the QStash success callback URL."""
        if not self.QSTASH_CALLBACK_BASE_URL:
            return None
        base = self.QSTASH_CALLBACK_BASE_URL.rstrip("/")
        return f"{base}/webhooks/qstash/callback"

    @property
    def qstash_failure_callback_url(self) -> Optional[str]:
        """Build the QStash failure callback URL."""
        if not self.QSTASH_CALLBACK_BASE_URL:
            return None
        base = self.QSTASH_CALLBACK_BASE_URL.rstrip("/")
        return f"{base}/webhooks/qstash/failure"
