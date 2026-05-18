"""Job state-history model."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class JobStateHistory(Base):
    """Job state-history model for individual state-machine transitions."""

    __tablename__ = "job_state_history"

    # Primary key.
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # Job association.
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )

    # Transition details.
    from_state: Mapped[str] = mapped_column(String(50), nullable=False)
    to_state: Mapped[str] = mapped_column(String(50), nullable=False)
    transition_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )  # Stored as JSON.

    # Timestamp.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    # Relationships.
    job: Mapped[Any] = relationship("Job", back_populates="state_history")

    # Indexes.
    __table_args__ = (
        Index("idx_job_state_history_job_id", "job_id"),
        Index("idx_job_state_history_created_at", "created_at"),
    )

    def __repr__(self):
        return f"<JobStateHistory(job_id={self.job_id}, {self.from_state}->{self.to_state})>"
