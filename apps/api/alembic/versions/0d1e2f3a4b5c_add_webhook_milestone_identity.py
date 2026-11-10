"""Retain one outbox identity per logical job milestone or indexing attempt."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0d1e2f3a4b5c"
down_revision: str | None = "180b9b67f2a7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    columns: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("webhook_events")}
    if "milestone_key" not in columns:
        op.add_column("webhook_events", sa.Column("milestone_key", sa.String(128), nullable=True))
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_milestone_key ON webhook_events (milestone_key)")


def downgrade() -> None:
    raise RuntimeError("Retain stable milestone identity for compatible backend rollback")
