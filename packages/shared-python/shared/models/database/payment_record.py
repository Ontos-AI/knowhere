"""
Payment Record Data Model (for idempotency guarantee)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive

class PaymentRecord(Base):
    """Payment Record Model (for idempotency guarantee)"""

    __tablename__ = "payment_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    payment_intent_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    checkout_session_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    # User Association
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # subscription/credits_package
    amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # Payment amount (cents)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # pending/succeeded/failed
    credits_amount: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )  # Micro-dollars (1 display credit = 1,000,000 micros)
    plan_id: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # Plan ID (subscription only)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )  # Stripe Subscription ID (subscription only)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )  # Processed time
    extra_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata", JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    # Relationship fields are intentionally omitted here.

    __table_args__ = (
        UniqueConstraint(
            "checkout_session_id", name="uq_payment_record_checkout_session_id"
        ),
    )

    def __repr__(self):
        return f"<PaymentRecord(id={self.id}, payment_intent_id='{self.payment_intent_id}', status='{self.status}')>"

    def is_subscription(self) -> bool:
        """Check if it's a subscription type"""
        return self.payment_type == "subscription"

    def is_credits_package(self) -> bool:
        """Check if it's a credits package type"""
        return self.payment_type == "credits_package"

    def is_succeeded(self) -> bool:
        """Check if processed successfully"""
        return self.status == "succeeded"
