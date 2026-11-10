"""Reader and native-operation ownership independent of SQL connection lifetime."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalLease(Base):
    __tablename__ = "lexical_operation_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    namespaceIndexId: Mapped[str] = mapped_column("namespace_index_id", String(64), ForeignKey("lexical_namespace_indexes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ownerToken: Mapped[str] = mapped_column("owner_token", String(36), nullable=False)
    backendEpoch: Mapped[int] = mapped_column("backend_epoch", Integer, nullable=False)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buildId: Mapped[str | None] = mapped_column("build_id", String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    expiresAt: Mapped[datetime] = mapped_column("expires_at", DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("idx_lexical_live_leases", "namespace_index_id", "kind", "state", "expires_at"),)
