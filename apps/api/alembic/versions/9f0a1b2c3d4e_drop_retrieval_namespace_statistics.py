"""Drop unused retrieval namespace statistics tables.

Query-time BM25 scoring reads only ``document_map_unit_tokens``,
``document_map_units``, and ``document_map_unit_indexes``. The per-revision and
namespace statistics tables were only written by publication/backfill and never
read by the retrieval path, so they are removed here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9f0a1b2c3d4e"
down_revision: str | None = "8e9f0a1b2c3d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

__all__ = [
    "revision",
    "down_revision",
    "branch_labels",
    "depends_on",
    "upgrade",
    "downgrade",
]


def upgrade() -> None:
    op.drop_index(
        "idx_retrieval_namespace_token_stats_lookup",
        table_name="retrieval_namespace_token_stats",
        if_exists=True,
    )
    op.drop_table("retrieval_namespace_token_stats", if_exists=True)
    op.drop_table("retrieval_namespace_stats", if_exists=True)
    op.drop_index(
        "idx_retrieval_serving_revision_stats_scope",
        table_name="retrieval_serving_revision_stats",
        if_exists=True,
    )
    op.drop_table("retrieval_serving_revision_stats", if_exists=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("retrieval_serving_revision_stats"):
        op.create_table(
            "retrieval_serving_revision_stats",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(length=255), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("job_result_id", sa.String(length=36), nullable=False),
            sa.Column("format_version", sa.Integer(), nullable=False),
            sa.Column("payload_zlib", sa.LargeBinary(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.document_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["job_result_id"], ["job_results.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "document_id",
                "job_result_id",
                name="uq_retrieval_serving_revision_stats_revision",
            ),
        )
        op.create_index(
            "idx_retrieval_serving_revision_stats_scope",
            "retrieval_serving_revision_stats",
            ["user_id", "namespace", "document_id", "job_result_id"],
        )
    if not inspector.has_table("retrieval_namespace_stats"):
        op.create_table(
            "retrieval_namespace_stats",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(length=255), nullable=False),
            sa.Column("generation", sa.BigInteger(), nullable=False),
            sa.Column("payload_zlib", sa.LargeBinary(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "namespace", name="uq_retrieval_namespace_stats_scope"
            ),
        )
    if not inspector.has_table("retrieval_namespace_token_stats"):
        op.create_table(
            "retrieval_namespace_token_stats",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(length=255), nullable=False),
            sa.Column("generation", sa.BigInteger(), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("document_frequency", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "namespace",
                "channel",
                "token_hash",
                name="uq_retrieval_namespace_token_stats_key",
            ),
        )
        op.create_index(
            "idx_retrieval_namespace_token_stats_lookup",
            "retrieval_namespace_token_stats",
            ["user_id", "namespace", "generation", "channel", "token_hash"],
        )
