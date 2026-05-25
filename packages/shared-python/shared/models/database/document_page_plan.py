"""Persisted page anatomy and future page-processing plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class DocumentPagePlan(Base):
    __tablename__ = "document_page_plan"

    page_plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shard_plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    global_signals: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        Index("idx_document_page_plan_job", "job_id"),
        Index("idx_document_page_plan_created", "created_at"),
    )
