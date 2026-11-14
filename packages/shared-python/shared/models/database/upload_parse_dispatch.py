from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.core.database import Base


class UploadParseDispatch(Base):
    """Durable Celery publication for one confirmed upload."""

    __tablename__ = "upload_parse_dispatches"
    __table_args__ = (
        Index(
            "ix_upload_parse_dispatches_due",
            "available_at",
            "job_id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"), primary_key=True
    )
    job_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
