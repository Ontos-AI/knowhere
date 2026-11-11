"""Chunk bodies and metadata shared by builds of the same revision."""

from sqlalchemy import JSON, ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalHydrationPayload(Base):
    __tablename__ = "lexical_hydration_payloads"

    revisionId: Mapped[str] = mapped_column(
        "revision_id",
        String(36),
        ForeignKey("job_results.id", ondelete="CASCADE"),
        primary_key=True,
    )
    payloadHash: Mapped[bytes] = mapped_column(
        "payload_hash", LargeBinary, primary_key=True
    )
    sectionPath: Mapped[str | None] = mapped_column("section_path", Text, nullable=True)
    chunkType: Mapped[str] = mapped_column("chunk_type", String(64), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sourceChunkPath: Mapped[str | None] = mapped_column(
        "source_chunk_path", Text, nullable=True
    )
    filePath: Mapped[str | None] = mapped_column("file_path", Text, nullable=True)
    chunkMetadata: Mapped[dict[str, object] | None] = mapped_column(
        "chunk_metadata", JSON, nullable=True
    )
