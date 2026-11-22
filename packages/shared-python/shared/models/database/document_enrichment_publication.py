"""Retained body and native index checkpoint for a single enrichment attempt."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class DocumentEnrichmentPublication(Base):
    __tablename__ = "document_enrichment_publications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False,
    )
    job_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_index_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    index_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cleanup_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('writing', 'published', 'failed')", name="ck_enrichment_publication_state"),
        Index("ix_enrichment_publication_revision", "job_result_id", "state"),
        Index("ix_enrichment_publication_cleanup", "cleanup_after"),
    )
