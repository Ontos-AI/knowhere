"""Record pending Celery publications for confirmed uploads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0132c4d5e6f"
down_revision: str | None = "af102b3c4d5e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_parse_dispatches",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("jobs.job_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("job_version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_upload_parse_dispatches_due",
        "upload_parse_dispatches",
        ["available_at", "job_id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("upload_parse_dispatches")
