"""Add persisted per-channel BM25 corpus statistics."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable statistics so incomplete legacy rows keep the fallback."""
    column_names: tuple[str, ...] = (
        "path_document_count",
        "path_total_length",
        "content_document_count",
        "content_total_length",
    )
    for column_name in column_names:
        op.execute(
            f"ALTER TABLE document_map_unit_indexes "
            f"ADD COLUMN IF NOT EXISTS {column_name} INTEGER"
        )


def downgrade() -> None:
    """Remove the additive statistics columns."""
    column_names: tuple[str, ...] = (
        "content_total_length",
        "content_document_count",
        "path_total_length",
        "path_document_count",
    )
    for column_name in column_names:
        op.execute(
            f"ALTER TABLE document_map_unit_indexes DROP COLUMN IF EXISTS {column_name}"
        )
