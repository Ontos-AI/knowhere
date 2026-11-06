"""Fence input collection and retain restartable object inventory work."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "3a4b5c6d7e8f"
down_revision: str | None = "2f3a4b5c6d7e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    existing: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("job_results")}
    columns: tuple[sa.Column, ...] = (
        sa.Column("indexing_input_gc_state", sa.String(16), nullable=False, server_default="retained"),
        sa.Column("indexing_input_next_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexing_input_gc_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexing_input_gc_error", sa.Text(), nullable=True),
        sa.Column("indexing_input_bytes", sa.BigInteger(), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("job_results", column)
    op.execute("CREATE INDEX IF NOT EXISTS idx_indexing_input_gc_scan ON job_results (indexing_input_next_scan_at, id) WHERE indexing_input_key IS NOT NULL AND indexing_input_gc_state <> 'deleted'")
    if not sa.inspect(op.get_bind()).has_table("lexical_input_inventory"):
        op.create_table("lexical_input_inventory",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("bucket", sa.String(255), nullable=False),
            sa.Column("object_key", sa.String(512), nullable=False),
            sa.Column("job_id", sa.String(36), nullable=True),
            sa.Column("revision_id", sa.String(36), nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="observed"),
            sa.Column("scan_after", sa.String(512), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("bucket", "object_key", name="uq_lexical_input_object"))
    op.execute("CREATE INDEX IF NOT EXISTS idx_lexical_input_scan ON lexical_input_inventory (state, next_scan_at)")


def downgrade() -> None:
    raise RuntimeError("Retain input deletion fences and inventory across compatible rollback")
