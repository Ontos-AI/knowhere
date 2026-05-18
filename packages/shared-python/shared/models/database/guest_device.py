"""Guest Device Model — tracks device-to-guest-user pairings for Knowhere Hub."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class GuestDevice(Base):
    """Pairs a client device_id to a guest user and their issued API key."""

    __tablename__ = "guest_devices"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    device_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, index=True
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    api_key_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    linked_user_id: Mapped[Optional[str]] = mapped_column(
        Text, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
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
        return f"<GuestDevice(id={self.id}, device_id='{self.device_id}', user_id='{self.user_id}')>"
