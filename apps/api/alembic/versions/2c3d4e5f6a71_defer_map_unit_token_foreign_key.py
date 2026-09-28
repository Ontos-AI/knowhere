"""Defer token to map-unit foreign-key validation until transaction commit."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "2c3d4e5f6a71"
down_revision: str | None = "2b3c4d5e6f70"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE_NAME = "document_map_unit_tokens"
_CONSTRAINT_NAME = "document_map_unit_tokens_map_unit_id_fkey"


def _is_deferrable() -> bool:
    result = op.get_bind().execute(
        text(
            "SELECT condeferrable FROM pg_constraint "
            "WHERE conrelid = CAST(:table_name AS regclass) "
            "AND conname = :constraint_name"
        ),
        {"table_name": _TABLE_NAME, "constraint_name": _CONSTRAINT_NAME},
    ).scalar_one_or_none()
    return bool(result)


def upgrade() -> None:
    """Keep referential integrity while batching token FK checks at commit."""
    if not _is_deferrable():
        op.execute(
            f"ALTER TABLE {_TABLE_NAME} ALTER CONSTRAINT {_CONSTRAINT_NAME} "
            "DEFERRABLE INITIALLY DEFERRED"
        )


def downgrade() -> None:
    """Restore immediate FK validation for rollback."""
    if _is_deferrable():
        op.execute(
            f"ALTER TABLE {_TABLE_NAME} ALTER CONSTRAINT {_CONSTRAINT_NAME} "
            "NOT DEFERRABLE"
        )
