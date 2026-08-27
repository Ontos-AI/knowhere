"""Add the chunk snapshot pagination order index.

Revision ID: fbf0c1d2e3f4
Revises: f0d85d209e68, fbe1c2d3e4f5
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "fbf0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = (
    "f0d85d209e68",
    "fbe1c2d3e4f5",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_document_chunks_revision_snapshot_order
        ON document_chunks (document_id, job_result_id, sort_order, chunk_id, id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_revision_snapshot_order")
