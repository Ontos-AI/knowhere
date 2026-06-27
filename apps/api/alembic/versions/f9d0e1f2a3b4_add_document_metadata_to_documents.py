"""Add document display metadata.

Revision ID: f9d0e1f2a3b4
Revises: f9c0d1e2f3a4
Create Date: 2026-06-26 04:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "f9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("document_metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "document_metadata")
