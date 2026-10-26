"""add document page embeddings

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-10-26 17:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "7a8b9c0d1e2f"
down_revision: str = "6f7a8b9c0d1e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "document_page_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("job_result_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("page_num", sa.Integer(), nullable=False),
        sa.Column("modality", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
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
            "page_num",
            name="uq_document_page_embeddings_revision_page",
        ),
    )
    op.create_index(
        "idx_document_page_embeddings_scope",
        "document_page_embeddings",
        ["user_id", "namespace", "job_result_id"],
    )
    op.create_index(
        "idx_document_page_embeddings_revision",
        "document_page_embeddings",
        ["document_id", "job_result_id", "page_num"],
    )
    op.create_index(
        "idx_document_page_embeddings_hnsw_cosine",
        "document_page_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "demo_document_page_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("namespace", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("job_result_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("page_num", sa.Integer(), nullable=False),
        sa.Column("modality", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["demo_documents.document_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["job_result_id"], ["job_results.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_result_id", "document_id"],
            ["job_results.id", "job_results.demo_document_id"],
            ondelete="RESTRICT",
            name="fk_demo_documentpageembedding_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "job_result_id",
            "page_num",
            name="uq_demo_document_page_embeddings_revision_page",
        ),
    )
    op.create_index(
        "idx_demo_document_page_embeddings_scope",
        "demo_document_page_embeddings",
        ["user_id", "namespace", "job_result_id"],
    )
    op.create_index(
        "idx_demo_document_page_embeddings_revision",
        "demo_document_page_embeddings",
        ["document_id", "job_result_id", "page_num"],
    )
    op.create_index(
        "idx_demo_document_page_embeddings_hnsw_cosine",
        "demo_document_page_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.execute("ALTER TABLE demo_document_page_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE demo_document_page_embeddings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY demo_maintainer_write ON demo_document_page_embeddings "
        "FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') "
        "WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '')"
    )
    op.execute(
        "CREATE POLICY demo_public_read ON demo_document_page_embeddings "
        "FOR SELECT USING (is_demo_revision_ready(document_id, job_result_id))"
    )


def downgrade() -> None:
    op.drop_table("demo_document_page_embeddings")
    op.drop_table("document_page_embeddings")
