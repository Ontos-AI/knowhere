"""
Webhook Models - Transactional Outbox Pattern

WebhookEvent: Represents the intent to send a webhook (the "outbox")
WebhookDelivery is handled by the existing WebhookLog model
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.core.time import utc_now_naive

if TYPE_CHECKING:
    from shared.models.database.job import Job
    from shared.models.database.webhook_log import WebhookLog


class WebhookEventStatus:
    """Webhook event status constants."""

    PENDING = "pending"  # Initial state, waiting for dispatch
    DELIVERING = "delivering"  # Currently being processed/retried
    DELIVERED = "delivered"  # Successfully delivered (terminal)
    FAILED = "failed"  # Max retries exceeded (terminal)
    CANCELED = "canceled"  # Manually canceled (terminal)


class WebhookEvent(Base):
    """
    WebhookEvent model - The Transactional Outbox

    Represents the intent to trigger a webhook notification.
    Created atomically with job status updates to prevent data loss.
    """

    __tablename__ = "webhook_events"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # Job association
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )

    # Webhook configuration (snapshot at creation time)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Payload (job result snapshot)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Delivery status
    status: Mapped[str] = mapped_column(
        String(50), default=WebhookEventStatus.PENDING, index=True, nullable=False
    )

    # Retry tracking
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # QStash tracking for the outbound delivery managed by Upstash.
    qstash_message_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", lazy="select")
    deliveries: Mapped[list["WebhookLog"]] = relationship(
        "WebhookLog",
        back_populates="event",
        cascade="all, delete-orphan",  # how about the manual trigger?
        foreign_keys="WebhookLog.event_id",
    )

    # Indexes for efficient queries
    __table_args__ = (
        Index("idx_webhook_events_job_id", "job_id"),
        Index("idx_webhook_events_status", "status"),
        Index("idx_webhook_events_next_retry", "next_retry_at"),
        Index("idx_webhook_events_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<WebhookEvent(id={self.id}, job_id={self.job_id}, status={self.status}, attempts={self.attempts})>"

    def is_terminal(self) -> bool:
        """Check if event is in a terminal state."""
        return self.status in (
            WebhookEventStatus.DELIVERED,
            WebhookEventStatus.FAILED,
            WebhookEventStatus.CANCELED,
        )

    def can_retry(self, max_attempts: int = 6) -> bool:
        """Check if event can be retried (not terminal and under max attempts)."""
        return not self.is_terminal() and self.attempts < max_attempts
