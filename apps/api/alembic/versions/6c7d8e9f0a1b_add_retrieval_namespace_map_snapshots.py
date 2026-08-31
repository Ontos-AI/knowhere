"""Add persisted namespace-level MAP snapshot table."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "6c7d8e9f0a1b"
down_revision: str | None = "5b6c7d8e9f0a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

__all__: list[str] = [
    "revision",
    "down_revision",
    "branch_labels",
    "depends_on",
    "upgrade",
    "downgrade",
]


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("retrieval_namespace_map_snapshots"):
        op.create_table(
            "retrieval_namespace_map_snapshots",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(length=255), nullable=False),
            sa.Column("generation", sa.BigInteger(), nullable=False),
            sa.Column("format_version", sa.Integer(), nullable=False),
            sa.Column("payload_zlib", sa.LargeBinary(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "namespace",
                name="uq_retrieval_namespace_map_snapshots_scope",
            ),
        )


def downgrade() -> None:
    op.drop_table("retrieval_namespace_map_snapshots", if_exists=True)
