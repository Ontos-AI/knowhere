"""Keep retained empty namespaces fair without changing eviction recency."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "4b5c6d7e8f90"
down_revision: str | None = "3a4b5c6d7e8f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    existing: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("lexical_namespace_indexes")}
    for column in (
        sa.Column("next_maintenance_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("maintenance_error", sa.Text(), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("lexical_namespace_indexes", column)
    op.execute("CREATE INDEX IF NOT EXISTS idx_lexical_namespace_maintenance ON lexical_namespace_indexes (state, next_maintenance_at, id)")


def downgrade() -> None:
    raise RuntimeError("Retain namespace maintenance history across compatible rollback")
