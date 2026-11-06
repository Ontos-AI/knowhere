"""Keep one physically fenced operator issuer per logical control authority."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "5c6d7e8f901a"
down_revision: str | None = "4b5c6d7e8f90"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("lexical_operator_issuers"):
        return
    op.create_table("lexical_operator_issuers",
        sa.Column("logical_cluster_id", sa.String(128), primary_key=True),
        sa.Column("control_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invocation_token", sa.String(36), nullable=False),
        sa.Column("host_identity", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("cluster_uuid", sa.String(64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence_receipt_sha256", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["control_id"], ["lexical_backend_control.id"], name="fk_lexical_operator_control"),
        sa.UniqueConstraint("control_id", name="uq_lexical_operator_control"),
        sa.UniqueConstraint("invocation_token", name="uq_lexical_operator_invocation"),
        sa.CheckConstraint("state IN ('active', 'unknown', 'released')", name="ck_lexical_operator_issuer_state"),
        sa.CheckConstraint("epoch >= 1", name="ck_lexical_operator_issuer_epoch"))


def downgrade() -> None:
    raise RuntimeError("Retain physical operator authority and fencing proof across compatible rollback")
