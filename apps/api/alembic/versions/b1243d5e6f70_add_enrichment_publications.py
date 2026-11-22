"""Keep enrichment bodies and outcomes alongside the published baseline."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1243d5e6f70"
down_revision: str | None = "b0132c4d5e6f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_enrichment_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_result_id", sa.String(36), sa.ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("expected_index_count", sa.Integer(), nullable=True),
        sa.Column("raw_prefix", sa.Text(), nullable=True),
        sa.Column("index_uuid", sa.String(128), nullable=True),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('writing', 'published', 'failed')", name="ck_enrichment_publication_state"),
    )
    op.create_index("ix_enrichment_publication_revision", "document_enrichment_publications", ["job_result_id", "state"])
    op.create_index("ix_enrichment_publication_cleanup", "document_enrichment_publications", ["cleanup_after"])
    op.add_column("document_chunks", sa.Column(
        "enrichment_attempt_id", sa.String(36),
        sa.ForeignKey("document_enrichment_publications.id", name="fk_document_chunks_enrichment_attempt", ondelete="CASCADE"),
    ))
    op.create_index("ix_document_chunks_enrichment_attempt_id", "document_chunks", ["enrichment_attempt_id"])
    op.create_index(
        "uq_document_chunks_baseline_path", "document_chunks",
        ["document_id", "job_result_id", "source_chunk_path"], unique=True,
        postgresql_where=sa.text("enrichment_attempt_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_document_chunks_attempt_path", "document_chunks", ["enrichment_attempt_id", "source_chunk_path"],
    )
    op.drop_constraint("uq_document_chunks_revision_path", "document_chunks", type_="unique")


def downgrade() -> None:
    # Preserve references to retained enrichment bodies across an application rollback.
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM document_enrichment_publications)")):
        raise RuntimeError("Cannot remove retained document enrichment publications")
    op.create_unique_constraint(
        "uq_document_chunks_revision_path", "document_chunks",
        ["document_id", "job_result_id", "source_chunk_path"],
    )
    op.drop_constraint("uq_document_chunks_attempt_path", "document_chunks", type_="unique")
    op.drop_index("uq_document_chunks_baseline_path", table_name="document_chunks")
    op.drop_index("ix_document_chunks_enrichment_attempt_id", table_name="document_chunks")
    op.drop_constraint("fk_document_chunks_enrichment_attempt", "document_chunks", type_="foreignkey")
    op.drop_column("document_chunks", "enrichment_attempt_id")
    op.drop_table("document_enrichment_publications")
