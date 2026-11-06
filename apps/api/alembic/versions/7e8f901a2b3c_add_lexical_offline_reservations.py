"""Retain offline snapshot restore capacity across physical issuer loss."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7e8f901a2b3c"
down_revision: str | None = "6d7e8f901a2b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("lexical_offline_reservations"):
        return
    op.create_table(
        "lexical_offline_reservations",
        sa.Column("target_cluster_uuid", sa.String(64), primary_key=True),
        sa.Column("index_name", sa.String(255), primary_key=True),
        sa.Column("target_incarnation", sa.String(64), nullable=False),
        sa.Column("source_cluster_uuid", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("issuer_token", sa.String(36), nullable=False),
        sa.Column("snapshot_uuid", sa.String(64), nullable=False),
        sa.Column("batch_reservation_id", sa.String(36), nullable=False),
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("boundary_sha256", sa.String(64), nullable=False),
        sa.Column("index_uuid", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["issuer_token"],
            ["lexical_operator_invocations.invocation_token"],
            name="fk_lexical_offline_issuer",
        ),
        sa.CheckConstraint(
            "state IN ('opening','open','closing','uncertain','closed')",
            name="ck_lexical_offline_state",
        ),
        sa.CheckConstraint(
            "target_cluster_uuid <> source_cluster_uuid",
            name="ck_lexical_offline_distinct_target",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Retain durable offline restore capacity across compatible rollback"
    )
