"""Durable indexing intent and bounded explicit-attempt history."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class IndexingAttempt(Base):
    __tablename__ = "document_indexing_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    jobId: Mapped[str] = mapped_column("job_id", String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False)
    revisionId: Mapped[str] = mapped_column("revision_id", String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False)
    attemptNumber: Mapped[int] = mapped_column("attempt_number", Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    executionCount: Mapped[int] = mapped_column("execution_count", Integer, nullable=False, default=0)
    backendEpoch: Mapped[int] = mapped_column("backend_epoch", Integer, nullable=False, default=1)
    currentBuildId: Mapped[str | None] = mapped_column("current_build_id", String(36), nullable=True)
    ownerToken: Mapped[str | None] = mapped_column("owner_token", String(36), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="initial", server_default="initial")
    admissionDeadlineAt: Mapped[datetime | None] = mapped_column("admission_deadline_at", DateTime(timezone=True), nullable=True)
    startedAt: Mapped[datetime | None] = mapped_column("started_at", DateTime(timezone=True), nullable=True)
    executionDeadlineAt: Mapped[datetime | None] = mapped_column("execution_deadline_at", DateTime(timezone=True), nullable=True)
    finishedAt: Mapped[datetime | None] = mapped_column("finished_at", DateTime(timezone=True), nullable=True)
    claimExpiresAt: Mapped[datetime | None] = mapped_column("claim_expires_at", DateTime(timezone=True), nullable=True)
    nextEligibleAt: Mapped[datetime] = mapped_column("next_eligible_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    nextDispatchAt: Mapped[datetime | None] = mapped_column("next_dispatch_at", DateTime(timezone=True), nullable=True)
    errorMessage: Mapped[str | None] = mapped_column("error_message", Text, nullable=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_indexing_attempt_number"),
        Index("uq_active_indexing_attempt", "job_id", unique=True, postgresql_where=text("state IN ('pending', 'running')")),
        Index("idx_indexing_dispatch", "state", "next_eligible_at"),
    )
