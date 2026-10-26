"""add document scope enrichments

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-10-26 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f7a8b9c0d1e"
down_revision: str = "5e6f7a8b9c0d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_scope_enrichments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("job_result_id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(), nullable=True),
        sa.Column("refined_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.document_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["job_result_id"], ["job_results.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_result_id",
            "scope_id",
            name="uq_document_scope_enrichments_revision_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("document_scope_enrichments")
