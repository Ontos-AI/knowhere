"""Canonical demo document materialization state."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class DemoMaterialization(Base):
    """Maps a canonical demo source to one user's retrieval document copy."""

    __tablename__ = "demo_materializations"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: f"demo_mat_{uuid4().hex[:12]}",
    )
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )
    demo_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
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
        UniqueConstraint(
            "user_id",
            "namespace",
            "demo_source_id",
            name="uq_demo_materializations_scope_source",
        ),
        Index("idx_demo_materializations_document", "document_id"),
    )
