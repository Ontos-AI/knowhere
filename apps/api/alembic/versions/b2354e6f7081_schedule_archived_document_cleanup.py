"""Schedule exact native cleanup after the archived state commits."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2354e6f7081"
down_revision: str | None = "b1243d5e6f70"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("index_cleanup_after", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_due_index_cleanup", "documents", ["index_cleanup_after"],
        postgresql_where=sa.text("index_cleanup_after IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_documents_due_index_cleanup", table_name="documents")
    op.drop_column("documents", "index_cleanup_after")
