"""Track immutable OpenSearch manifests and the chunk rows addressed by a build."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "af102b3c4d5e"
down_revision: str | None = "9e0f1a2b3c4d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lexical_namespace_indexes", sa.Column("published_manifest_id", sa.String(100), nullable=True))
    op.add_column("document_indexing_builds", sa.Column(
        "retain_for_manifest_reads", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column("document_indexing_builds", sa.Column(
        "manifest_cleanup_after", sa.DateTime(timezone=True), nullable=True,
    ))
    op.create_table(
        "lexical_published_manifests",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("namespace_index_id", sa.String(64), sa.ForeignKey("lexical_namespace_indexes.id"), nullable=False),
        sa.Column("generation", sa.Integer()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("cleanup_after", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_lexical_published_manifests_cleanup", "lexical_published_manifests", ["state", "cleanup_after"])
    op.create_table(
        "lexical_hydration_snapshots",
        sa.Column("build_id", sa.String(36), sa.ForeignKey("document_indexing_builds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_file_name", sa.Text()),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("result_raw_prefix", sa.Text()),
    )
    op.create_table(
        "lexical_hydration_payloads",
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("job_results.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("payload_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("section_path", sa.Text()),
        sa.Column("chunk_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("source_chunk_path", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("chunk_metadata", sa.JSON()),
    )
    op.create_table(
        "lexical_hydration_rows",
        sa.Column("build_id", sa.String(36), sa.ForeignKey("lexical_hydration_snapshots.build_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_row_id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("payload_hash", sa.LargeBinary(), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("section_id", sa.String(36)),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id", "payload_hash"],
            ["lexical_hydration_payloads.revision_id", "lexical_hydration_payloads.payload_hash"], ondelete="CASCADE"),
    )
    op.create_index("idx_lexical_hydration_rows_section", "lexical_hydration_rows", ["build_id", "section_id"])
    op.create_index("idx_lexical_hydration_rows_chunk", "lexical_hydration_rows", ["build_id", "chunk_id"])
    op.create_index("idx_lexical_hydration_rows_payload", "lexical_hydration_rows", ["revision_id", "payload_hash"])


def downgrade() -> None:
    op.drop_table("lexical_hydration_rows")
    op.drop_table("lexical_hydration_payloads")
    op.drop_table("lexical_hydration_snapshots")
    op.drop_table("lexical_published_manifests")
    op.drop_column("document_indexing_builds", "manifest_cleanup_after")
    op.drop_column("document_indexing_builds", "retain_for_manifest_reads")
    op.drop_column("lexical_namespace_indexes", "published_manifest_id")
