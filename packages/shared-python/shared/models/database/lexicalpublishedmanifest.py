"""Lifecycle of an immutable OpenSearch list of published builds."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalPublishedManifest(Base):
    __tablename__ = "lexical_published_manifests"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    namespaceIndexId: Mapped[str] = mapped_column(
        "namespace_index_id", String(64), ForeignKey("lexical_namespace_indexes.id"), nullable=False
    )
    generation: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    cleanupAfter: Mapped[datetime | None] = mapped_column("cleanup_after", DateTime(timezone=True))

    __table_args__ = (Index("idx_lexical_published_manifests_cleanup", "state", "cleanup_after"),)
