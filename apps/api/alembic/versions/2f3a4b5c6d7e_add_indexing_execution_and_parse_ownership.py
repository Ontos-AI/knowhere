"""Persist single-execution deadlines and fence parsing owners."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "2f3a4b5c6d7e"
down_revision: str | None = "1e2f3a4b5c6d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    tables: dict[str, tuple[sa.Column, ...]] = {
        "jobs": (
            sa.Column("parsing_owner_token", sa.String(36), nullable=True),
            sa.Column("parsing_owner_expires_at", sa.DateTime(timezone=True), nullable=True),
        ),
        "document_indexing_attempts": (
            sa.Column("origin", sa.String(16), nullable=False, server_default="initial"),
            sa.Column("admission_deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        ),
    }
    for table, columns in tables.items():
        existing: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    op.execute("UPDATE document_indexing_attempts SET origin='manual' WHERE attempt_number > 1 AND origin='initial'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_indexing_settlement ON document_indexing_attempts (state, execution_deadline_at, admission_deadline_at)")


def downgrade() -> None:
    raise RuntimeError("Retain execution history and parsing fences for compatible rollback")
