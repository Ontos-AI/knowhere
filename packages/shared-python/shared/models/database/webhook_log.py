"""
Webhook Delivery History Model

Records every webhook delivery attempt for auditing and debugging.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class WebhookLog(Base):
    """Webhook Log Model - Records webhook delivery history."""

    __tablename__ = "webhook_logs"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # Associations
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=True
    )  # Optional for backward compatibility

    # Webhook information
    webhook_url: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # Attempt number (1-6)

    # Request information
    request_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )  # JSON storage
    signature: Mapped[str] = mapped_column(
        String(128), nullable=False
    )  # HMAC signature
    idempotency_key: Mapped[str] = mapped_column(
        String(36), nullable=False
    )  # UUID idempotency key

    # Response information
    response_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # Request duration in milliseconds

    # Delivery provider tracking
    delivery_provider: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, server_default="self"
    )
    qstash_message_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    # Relationships
    job: Mapped[Any] = relationship("Job", back_populates="webhook_logs")
    event: Mapped[Optional[Any]] = relationship(
        "WebhookEvent", back_populates="deliveries", foreign_keys=[event_id]
    )

    # Indexes
    __table_args__ = (
        Index("idx_webhook_logs_job_id", "job_id"),
        Index("idx_webhook_logs_event_id", "event_id"),
        Index("idx_webhook_logs_created_at", "created_at"),
        Index("idx_webhook_logs_attempt", "job_id", "attempt_number"),
    )

    def __repr__(self):
        return f"<WebhookLog(job_id={self.job_id}, attempt={self.attempt_number}, status={self.response_status_code})>"

    def is_success(self) -> bool:
        """Check if the request was successful."""
        return (
            self.response_status_code is not None
            and 200 <= self.response_status_code < 300
        )

    def is_failed(self) -> bool:
        """Check if the request failed."""
        return self.response_status_code is None or self.response_status_code >= 400
