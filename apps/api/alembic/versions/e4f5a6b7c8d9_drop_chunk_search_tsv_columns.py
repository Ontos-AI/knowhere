"""Drop unused PostgreSQL FTS columns on document_chunks.

Retrieval no longer reads ``content_search_tsv`` / ``path_search_tsv``.
They were generated from ``content_search_text`` / ``path_search_text`` and
only served the retired FTS fallback.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d3e4f5a6b7c8"
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
    op.execute("DROP INDEX IF EXISTS idx_chunk_path_search_tsv")
    op.execute("DROP INDEX IF EXISTS idx_chunk_content_search_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS path_search_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_search_tsv")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN content_search_tsv TSVECTOR "
        "GENERATED ALWAYS AS (to_tsvector('simple', COALESCE(content_search_text, ''))) "
        "STORED"
    )
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN path_search_tsv TSVECTOR "
        "GENERATED ALWAYS AS (to_tsvector('simple', COALESCE(path_search_text, ''))) "
        "STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_content_search_tsv "
        "ON document_chunks USING GIN (content_search_tsv)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_path_search_tsv "
        "ON document_chunks USING GIN (path_search_tsv)"
    )
