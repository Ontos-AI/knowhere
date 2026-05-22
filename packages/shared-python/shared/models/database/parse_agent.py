"""Parse-side agent trace models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base
from shared.utils.utc_now import utc_now_naive


class ParseRun(Base):
    __tablename__ = "parse_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="profile")
    final_status: Mapped[str] = mapped_column(String(32), nullable=False)
    rounds_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=utc_now_naive, nullable=True
    )

    __table_args__ = (
        Index("idx_parse_runs_job_kind", "job_id", "kind"),
        Index("idx_parse_runs_started", "started_at"),
    )


class ParseStep(Base):
    __tablename__ = "parse_steps"

    step_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("parse_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    round_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tool_args: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    observation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now_naive, nullable=False
    )

    __table_args__ = (
        Index("idx_parse_steps_run_round", "run_id", "round_index"),
        Index("idx_parse_steps_tool", "tool_name"),
    )
