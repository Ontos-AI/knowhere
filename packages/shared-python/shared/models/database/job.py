"""
Job Data Model - User API Business Task
"""

from __future__ import annotations

from datetime import datetime

from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class Job(Base):
    """Job Model - User API Business Task"""

    __tablename__ = "jobs"

    # Primary Key
    job_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )

    # User Association
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Basic Job Info
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)  # document_ingestion
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending"
    )  # PRD Status: pending, waiting-file, running, converting, done, failed

    # File Info
    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # direct_upload, url
    file_path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )  # Original file path
    s3_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)  # S3 Key

    # Webhook Config
    webhook_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    webhook_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Metadata and error information
    job_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )  # JSON storage
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # Canonical error code (e.g., INVALID_ARGUMENT, INTERNAL_ERROR)

    # Version Control (Optimistic Lock)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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

    # Billing Information (Per-Page Billing)
    page_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="Calculated page count for billing"
    )
    credits_charged: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="In micro-dollars: $1.00 = 1,000,000",
    )
    billing_status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
        comment="pending, charged, billing_failed, refunded, skipped",
    )

    # Relationships — default to noload to prevent implicit SELECTs.
    # Use explicit selectinload() in queries that need related data.
    state_history: Mapped[list[Any]] = relationship(
        "JobStateHistory",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    state_audit_logs: Mapped[list[Any]] = relationship(
        "JobStateAuditLog",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="noload",
    )
    webhook_logs: Mapped[list[Any]] = relationship(
        "WebhookLog", back_populates="job", cascade="all, delete-orphan", lazy="noload"
    )
    job_result: Mapped[Optional[Any]] = relationship(
        "JobResult", back_populates="job", uselist=False, lazy="noload"
    )

    # Indexes
    __table_args__ = (
        Index("idx_job_status", "status"),
        Index("idx_job_type", "job_type"),
        Index("idx_job_created_at", "created_at"),
        Index("idx_job_user_status", "user_id", "status"),
        Index(
            "idx_job_user_active_states",
            "user_id",
            "status",
            postgresql_where=text(
                "status IN ('waiting-file', 'pending', 'running', 'converting')"
            ),
        ),
        Index(
            "idx_job_active_updated_at",
            "status",
            "updated_at",
            postgresql_where=text(
                "status IN ('waiting-file', 'pending', 'running', 'converting')"
            ),
        ),
        Index(
            "uq_jobs_user_active_document",
            "user_id",
            text("(job_metadata ->> 'document_id')"),
            unique=True,
            postgresql_where=text(
                "status IN ('waiting-file', 'pending', 'running', 'converting') "
                "AND (job_metadata ->> 'document_id') IS NOT NULL"
            ),
        ),
    )

    def __repr__(self):
        return f"<Job(job_id={self.job_id}, type='{self.job_type}', status='{self.status}')>"

    def is_terminal_state(self) -> bool:
        """Check if terminal state"""
        return self.status in ["done", "failed"]

    def is_processing(self) -> bool:
        """Check if processing"""
        return self.status == "running"
