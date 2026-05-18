"""Job-result and chunk data models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.core.database import Base
from shared.core.time import utc_now_naive

if TYPE_CHECKING:
    from shared.models.database.job import Job


class JobResult(Base):
    """Primary job-result table."""

    __tablename__ = "job_results"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    document_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    delivery_mode: Mapped[str] = mapped_column(String(20), nullable=False)  # inline/url
    document_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    inline_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    result_s3_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    result_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        nullable=False,
    )

    job: Mapped["Job"] = relationship(
        "Job", back_populates="job_result", lazy="joined"
    )
    chunks: Mapped[List["JobChunk"]] = relationship(
        "JobChunk",
        back_populates="job_result",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<JobResult(id={self.id}, job_id={self.job_id}, delivery_mode={self.delivery_mode})>"


class JobChunk(Base):
    """Chunk details for a parsed job result."""

    __tablename__ = "job_chunks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False
    )

    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(2000), nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    chunk_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    job_result: Mapped["JobResult"] = relationship("JobResult", back_populates="chunks")

    __table_args__ = (
        Index("idx_job_chunks_result", "job_result_id"),
        Index("idx_job_chunks_chunk_id", "chunk_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<JobChunk(chunk_id={self.chunk_id}, job_result_id={self.job_result_id})>"
        )
