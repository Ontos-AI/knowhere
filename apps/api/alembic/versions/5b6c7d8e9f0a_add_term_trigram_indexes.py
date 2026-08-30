"""Add trigram acceleration for exact term-channel candidate discovery."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "5b6c7d8e9f0a"
down_revision: str | None = "4a5b6c7d8e9f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_MAP_UNIT_INDEX = "idx_document_map_units_term_trgm"
_CHUNK_INDEX = "idx_document_chunks_term_trgm"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_MAP_UNIT_INDEX} "
        "ON document_map_units USING gin "
        "(term_search_text_lower gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_CHUNK_INDEX} "
        "ON document_chunks USING gin "
        "(lower(COALESCE(term_search_text, '')) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_CHUNK_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_MAP_UNIT_INDEX}")
