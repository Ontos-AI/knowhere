"""Immutable artifact locations and capture marker for one native build."""

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class LexicalHydrationSnapshot(Base):
    __tablename__ = "lexical_hydration_snapshots"

    buildId: Mapped[str] = mapped_column(
        "build_id",
        String(36),
        ForeignKey("document_indexing_builds.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sourceFileName: Mapped[str | None] = mapped_column(
        "source_file_name", Text, nullable=True
    )
    jobId: Mapped[str] = mapped_column("job_id", String(36), nullable=False)
    resultRawPrefix: Mapped[str | None] = mapped_column(
        "result_raw_prefix", Text, nullable=True
    )
