"""
User Balance data model
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.core.time import utc_now_naive

class UserBalance(Base):
    """User balance model — tracks credits balance and tier membership"""

    __tablename__ = "user_balances"

    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), primary_key=True
    )
    credits_balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    user_tier: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
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

    def __repr__(self) -> str:
        return f"<UserBalance(user_id='{self.user_id}', tier='{self.user_tier}')>"
