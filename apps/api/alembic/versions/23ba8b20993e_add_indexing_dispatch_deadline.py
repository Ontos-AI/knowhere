"""Bound duplicate dispatch while retaining database-backed indexing intent."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "23ba8b20993e"
down_revision: str | None = "d74fe2bd581c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    columns: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("document_indexing_attempts")}
    if "next_dispatch_at" not in columns:
        op.add_column("document_indexing_attempts", sa.Column("next_dispatch_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Retain additive indexing state for compatible backend rollback")
