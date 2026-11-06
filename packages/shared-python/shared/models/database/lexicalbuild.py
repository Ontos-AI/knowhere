"""One physical lexical build per claimed execution, with durable cleanup state."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalBuild(Base):
    __tablename__ = "document_indexing_builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    attemptId: Mapped[str] = mapped_column("attempt_id", String(36), ForeignKey("document_indexing_attempts.id", ondelete="CASCADE"), nullable=False)
    namespaceIndexId: Mapped[str] = mapped_column("namespace_index_id", String(64), ForeignKey("lexical_namespace_indexes.id"), nullable=False)
    documentId: Mapped[str] = mapped_column("document_id", String(36), nullable=False)
    revisionId: Mapped[str] = mapped_column("revision_id", String(36), ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False)
    ownerToken: Mapped[str] = mapped_column("owner_token", String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="writing")
    unitCount: Mapped[int] = mapped_column("unit_count", Integer, nullable=False, default=0)
    activationGeneration: Mapped[int | None] = mapped_column("activation_generation", Integer, nullable=True)
    retirementGeneration: Mapped[int | None] = mapped_column("retirement_generation", Integer, nullable=True)
    cleanupState: Mapped[str] = mapped_column("cleanup_state", String(32), nullable=False, default="not_started")
    legacyCopyState: Mapped[str] = mapped_column("legacy_copy_state", String(32), nullable=False, default="not_started")
    nextMaintenanceAt: Mapped[datetime | None] = mapped_column("next_maintenance_at", DateTime(timezone=True), nullable=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (Index("idx_lexical_build_scope", "namespace_index_id", "state"),)
