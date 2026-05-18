"""Job state-transition audit log model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive

if TYPE_CHECKING:
    from shared.models.database.job import Job


class JobStateAuditLog(Base):
    """Job state-transition audit log."""

    __tablename__ = "job_state_audit_logs"

    # Primary key.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Job association.
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )

    # Transition details.
    from_state: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # Source state.
    to_state: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # Destination state.
    transition_reason: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # Transition reason.

    # Operator details.
    operator_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )  # Operator ID.
    operator_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="system"
    )  # Operator type: system, user, retry.

    # Transition metadata.
    transition_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )  # Extra data captured during the transition.

    # Timestamp.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    # Relationships.
    job: Mapped["Job"] = relationship(
        "Job", back_populates="state_audit_logs", lazy="select"
    )

    # Indexes.
    __table_args__ = (
        Index("idx_audit_log_job_id", "job_id"),
        Index("idx_audit_log_created_at", "created_at"),
        Index("idx_audit_log_job_created", "job_id", "created_at"),
    )

    def __repr__(self):
        return f"<JobStateAuditLog(job_id={self.job_id}, {self.from_state}->{self.to_state})>"
