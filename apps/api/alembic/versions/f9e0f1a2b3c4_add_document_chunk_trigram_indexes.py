"""Add document chunk trigram indexes.

Revision ID: f9e0f1a2b3c4
Revises: f9d0e1f2a3b4
Create Date: 2026-06-24 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "f9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX idx_document_chunks_content_trgm
        ON document_chunks USING GIN (content gin_trgm_ops)
        WHERE content IS NOT NULL AND content <> ''
        """
    )
    op.execute(
        """
        CREATE INDEX idx_document_chunks_lower_content_trgm
        ON document_chunks USING GIN (lower(content) gin_trgm_ops)
        WHERE content IS NOT NULL AND content <> ''
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_lower_content_trgm")
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_content_trgm")
