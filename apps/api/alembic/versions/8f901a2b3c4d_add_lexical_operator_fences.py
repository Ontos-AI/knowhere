"""Reserve one durable physical fence before any external recovery command."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "8f901a2b3c4d"
down_revision: str | None = "7e8f901a2b3c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("lexical_operator_fences"):
        return
    op.create_table("lexical_operator_fences",
        sa.Column("old_invocation_token", sa.String(36), primary_key=True),
        sa.Column("fence_token", sa.String(36), nullable=False),
        sa.Column("old_binding_sha256", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("control_backend", sa.String(32), nullable=False),
        sa.Column("node_incarnation", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("proof_sha256", sa.String(64), nullable=True),
        sa.Column("proven_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["old_invocation_token"], ["lexical_operator_invocations.invocation_token"],
            name="fk_lexical_fence_old_invocation"),
        sa.UniqueConstraint("fence_token", name="uq_lexical_fence_token"),
        sa.CheckConstraint("epoch >= 1", name="ck_lexical_fence_epoch"),
        sa.CheckConstraint("(state='pending' AND proof_sha256 IS NULL AND proven_at IS NULL AND consumed_at IS NULL)"
            " OR (state='proven' AND proof_sha256 IS NOT NULL AND proven_at IS NOT NULL AND consumed_at IS NULL)"
            " OR (state='consumed' AND proof_sha256 IS NOT NULL AND proven_at IS NOT NULL AND consumed_at IS NOT NULL)",
            name="ck_lexical_fence_progress"))


def downgrade() -> None:
    raise RuntimeError("Retain unresolved physical fences and consumed takeover proof across compatible rollback")
