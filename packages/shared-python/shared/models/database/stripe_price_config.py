"""Stripe price configuration model."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class StripePriceConfig(Base):
    """Persisted Stripe price configuration."""

    __tablename__ = "stripe_price_configs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    price_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    product_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # subscription/credits_package
    plan_id: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # plus/pro/credits_500 and similar plan IDs
    credits_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )  # Micro-credits (1 display credit = 1,000,000 micros)
    amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )  # Amount in cents, required for validation and UI display.
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("price_id", name="uq_stripe_price_config_price_id"),
        CheckConstraint(
            "(product_type = 'credits_package' AND credits_amount > 0) OR (product_type = 'subscription')",
            name="chk_credits_package_has_amount",
        ),
        CheckConstraint(
            "amount_cents >= 0",
            name="chk_amount_cents_non_negative",
        ),
    )

    def __repr__(self):
        return f"<StripePriceConfig(id={self.id}, price_id='{self.price_id}', product_type='{self.product_type}', plan_id='{self.plan_id}')>"

    def is_subscription(self) -> bool:
        """Return whether the price is a subscription."""
        return self.product_type == "subscription"

    def is_credits_package(self) -> bool:
        """Return whether the price is a credits package."""
        return self.product_type == "credits_package"
