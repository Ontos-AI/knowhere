"""Exact chunk membership and ordering of one captured native build."""

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalHydrationRow(Base):
    __tablename__ = "lexical_hydration_rows"

    buildId: Mapped[str] = mapped_column(
        "build_id",
        String(36),
        ForeignKey("lexical_hydration_snapshots.build_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sourceRowId: Mapped[str] = mapped_column(
        "source_row_id", String(36), primary_key=True
    )
    revisionId: Mapped[str] = mapped_column("revision_id", String(36), nullable=False)
    payloadHash: Mapped[bytes] = mapped_column(
        "payload_hash", LargeBinary, nullable=False
    )
    chunkId: Mapped[str] = mapped_column("chunk_id", String(64), nullable=False)
    sectionId: Mapped[str | None] = mapped_column(
        "section_id", String(36), nullable=True
    )
    sortOrder: Mapped[int] = mapped_column("sort_order", Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ("revision_id", "payload_hash"),
            (
                "lexical_hydration_payloads.revision_id",
                "lexical_hydration_payloads.payload_hash",
            ),
            ondelete="CASCADE",
        ),
        Index("idx_lexical_hydration_rows_section", "build_id", "section_id"),
        Index("idx_lexical_hydration_rows_chunk", "build_id", "chunk_id"),
        Index("idx_lexical_hydration_rows_payload", "revision_id", "payload_hash"),
    )
