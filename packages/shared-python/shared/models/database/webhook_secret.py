"""
Webhook Secret Model - Encrypted signing secrets for webhooks.

Stores per-user secrets with optional endpoint-specific overrides.
All secrets are encrypted at rest using Fernet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class WebhookSecretStatus:
    """Webhook secret status constants."""

    ACTIVE = "active"
    REVOKED = "revoked"
    ROTATED = "rotated"


class WebhookSecret(Base):
    """
    WebhookSecret model - Stores encrypted webhook signing secrets.

    Secrets are stored per-user with optional endpoint-specific overrides.
    All secrets are encrypted at rest using Fernet.
    """

    __tablename__ = "webhook_secrets"

    # Primary key with prefix "ws_"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: f"ws_{uuid4().hex[:24]}"
    )

    # User association
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )

    # Optional endpoint-specific secret (NULL = default account secret)
    endpoint: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)

    # Encrypted secret value (Fernet token)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # Status
    status: Mapped[str] = mapped_column(
        String(20), default=WebhookSecretStatus.ACTIVE, nullable=False
    )

    # Additional metadata
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    # Indexes
    __table_args__ = (
        Index("idx_webhook_secrets_user_id", "user_id"),
        Index("idx_webhook_secrets_user_endpoint", "user_id", "endpoint"),
        Index("idx_webhook_secrets_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<WebhookSecret(id={self.id}, user_id={self.user_id}, endpoint={self.endpoint}, status={self.status})>"

    def is_active(self) -> bool:
        """Check if secret is active."""
        return self.status == WebhookSecretStatus.ACTIVE
