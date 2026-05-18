"""
API Key Data Model
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class APIKey(Base):
    """API Key Model"""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )  # Encrypted storage
    key_mask: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # Masked API Key (for display)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # API Key Name
    enabled_modules: Mapped[Optional[List[str]]] = mapped_column(
        JSON, nullable=True
    )  # Enabled functional modules
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )  # Expiration time
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )  # Active status
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )  # Last used time

    # Relationship fields are intentionally omitted here for now.

    def __repr__(self):
        return f"<APIKey(id={self.id}, name='{self.name}', user_id='{self.user_id}')>"

    def is_expired(self) -> bool:
        """Check if expired"""
        if self.expires_at is None:
            return False
        return utc_now_naive() > self.expires_at

    def is_valid(self) -> bool:
        """Check if valid"""
        return self.is_active and not self.is_expired()
