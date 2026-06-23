"""Add persisted document chunk ordinals.

Revision ID: f9d0e1f2a3b4
Revises: f9c0d1e2f3a4
Create Date: 2026-06-23 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "f9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("ordinal", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        WITH ordered_chunks AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY document_id, job_result_id
                    ORDER BY sort_order ASC, created_at ASC, id ASC
                ) AS ordinal
            FROM document_chunks
        )
        UPDATE document_chunks dc
        SET ordinal = ordered_chunks.ordinal
        FROM ordered_chunks
        WHERE ordered_chunks.id = dc.id
        """
    )
    op.alter_column("document_chunks", "ordinal", nullable=False)
    op.create_index(
        "uq_document_chunks_revision_ordinal",
        "document_chunks",
        ["document_id", "job_result_id", "ordinal"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_document_chunks_revision_ordinal", table_name="document_chunks")
    op.drop_column("document_chunks", "ordinal")
