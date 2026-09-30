"""Drop unused map-unit term-search leftovers.

Classic scoring is path + content only. ``term_search_text_lower`` on
``document_map_units`` and its trigram index had no reader. Chunk
``term_search_text`` and ``idx_document_chunks_term_trgm`` stay: grep uses them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "2b3c4d5e6f7a"
down_revision: str | None = "2f3g4h5i6j7"
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

_MAP_UNIT_INDEX = "idx_document_map_units_term_trgm"


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_MAP_UNIT_INDEX}")
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("document_map_units")}
    if "term_search_text_lower" in columns:
        op.drop_column("document_map_units", "term_search_text_lower")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("document_map_units")}
    if "term_search_text_lower" not in columns:
        op.add_column(
            "document_map_units",
            sa.Column("term_search_text_lower", sa.Text(), nullable=False, server_default=""),
        )
        op.alter_column("document_map_units", "term_search_text_lower", server_default=None)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_MAP_UNIT_INDEX} "
        "ON document_map_units USING gin "
        "(term_search_text_lower gin_trgm_ops)"
    )
