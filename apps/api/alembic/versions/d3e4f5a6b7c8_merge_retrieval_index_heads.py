"""Merge the retrieval index migration branches."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "d3e4f5a6b7c8"
down_revision: tuple[str, str] = (
    "0a1b2c3d4e5f",
    "c2d3e4f5a6b7",
)
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Merge migration heads without applying additional schema changes."""


def downgrade() -> None:
    """Split the migration graph back into its two parent heads."""
