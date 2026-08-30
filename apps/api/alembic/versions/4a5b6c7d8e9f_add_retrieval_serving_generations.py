"""Add revision-pinned serving manifests and namespace statistics."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "4a5b6c7d8e9f"
down_revision: str | None = "3f4a5b6c7d8e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("retrieval_namespace_generations"):
        op.create_table(
            "retrieval_namespace_generations",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(length=255), nullable=False),
            sa.Column(
                "generation", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "namespace",
                name="uq_retrieval_namespace_generations_scope",
            ),
        )
    if not sa.inspect(op.get_bind()).has_table("retrieval_serving_revision_manifests"):
        op.create_table(
            "retrieval_serving_revision_manifests",
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
                name="uq_retrieval_serving_revision_manifests_revision",
            ),
        )
    if not _index_exists("idx_retrieval_serving_revision_manifests_scope"):
        op.create_index(
            "idx_retrieval_serving_revision_manifests_scope",
            "retrieval_serving_revision_manifests",
            ["user_id", "namespace", "document_id", "job_result_id"],
        )
    if not sa.inspect(op.get_bind()).has_table("retrieval_serving_revision_stats"):
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
    if not _index_exists("idx_retrieval_serving_revision_stats_scope"):
        op.create_index(
            "idx_retrieval_serving_revision_stats_scope",
            "retrieval_serving_revision_stats",
            ["user_id", "namespace", "document_id", "job_result_id"],
        )
    if not sa.inspect(op.get_bind()).has_table("retrieval_namespace_stats"):
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
    if not sa.inspect(op.get_bind()).has_table("retrieval_namespace_token_stats"):
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
    if not _index_exists("idx_retrieval_namespace_token_stats_lookup"):
        op.create_index(
            "idx_retrieval_namespace_token_stats_lookup",
            "retrieval_namespace_token_stats",
            ["user_id", "namespace", "generation", "channel", "token_hash"],
        )


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table_name in inspector.get_table_names():
        if any(
            index.get("name") == index_name
            for index in inspector.get_indexes(table_name)
        ):
            return True
    return False


def downgrade() -> None:
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
    op.drop_index(
        "idx_retrieval_serving_revision_manifests_scope",
        table_name="retrieval_serving_revision_manifests",
        if_exists=True,
    )
    op.drop_table("retrieval_serving_revision_manifests", if_exists=True)
    op.drop_table("retrieval_namespace_generations", if_exists=True)
