"""Durable native index identity and admission reservation."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalNamespaceIndex(Base):
    __tablename__ = "lexical_namespace_indexes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ownerId: Mapped[str] = mapped_column("owner_id", Text, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    corpusKind: Mapped[str] = mapped_column("corpus_kind", String(16), nullable=False)
    indexName: Mapped[str] = mapped_column("index_name", String(128), nullable=False, unique=True)
    indexUuid: Mapped[str | None] = mapped_column("index_uuid", String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="uncreated")
    operationId: Mapped[str | None] = mapped_column("operation_id", String(36), nullable=True)
    lastUsedAt: Mapped[datetime] = mapped_column("last_used_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    nextMaintenanceAt: Mapped[datetime | None] = mapped_column("next_maintenance_at", DateTime(timezone=True), nullable=True)
    maintenanceError: Mapped[str | None] = mapped_column("maintenance_error", Text, nullable=True)
    __table_args__ = (UniqueConstraint("corpus_kind", "owner_id", "namespace", name="uq_lexical_namespace_scope"),)
