"""Allow blocked tombstones to yield maintenance capacity to newer work."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "180b9b67f2a7"
down_revision: str | None = "23ba8b20993e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    columns: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("document_indexing_builds")}
    if "next_maintenance_at" not in columns:
        op.add_column("document_indexing_builds", sa.Column("next_maintenance_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Retain additive maintenance state for compatible backend rollback")
