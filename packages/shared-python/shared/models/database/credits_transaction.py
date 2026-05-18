"""
Credits Transaction Data Model
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class CreditsTransaction(Base):
    """Credits Transaction Model"""

    __tablename__ = "credits_transactions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    credits_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )  # In micro-dollars: $1.00 = 1,000,000; positive=add, negative=deduct
    transaction_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # purchase, usage, bonus, refund
    stripe_payment_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    transaction_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False, index=True
    )

    # Relationship fields are intentionally omitted here for now.
    # user: Mapped[User] = relationship("User", back_populates="credits_transactions", lazy="select")

    def __repr__(self):
        return f"<CreditsTransaction(id={self.id}, amount={self.credits_amount}, type='{self.transaction_type}')>"

    def is_credit(self) -> bool:
        """Check if it adds credits"""
        return self.credits_amount > 0

    def is_debit(self) -> bool:
        """Check if it deducts credits"""
        return self.credits_amount < 0
