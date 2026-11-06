"""Durable input-object tombstones and a restartable orphan inventory cursor."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalInputInventory(Base):
    __tablename__ = "lexical_input_inventory"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    objectKey: Mapped[str] = mapped_column("object_key", String(512), nullable=False)
    jobId: Mapped[str | None] = mapped_column("job_id", String(36), nullable=True)
    revisionId: Mapped[str | None] = mapped_column("revision_id", String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="observed")
    scanAfter: Mapped[str | None] = mapped_column("scan_after", String(512), nullable=True)
    sizeBytes: Mapped[int | None] = mapped_column("size_bytes", BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastError: Mapped[str | None] = mapped_column("last_error", Text, nullable=True)
    firstSeenAt: Mapped[datetime] = mapped_column("first_seen_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    nextScanAt: Mapped[datetime] = mapped_column("next_scan_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # No FK: objects uploaded before reference assignment and objects whose
    # canonical result was removed must remain discoverable/retryable.
    __table_args__ = (
        UniqueConstraint("bucket", "object_key", name="uq_lexical_input_object"),
        Index("idx_lexical_input_scan", "state", "next_scan_at"),
    )
