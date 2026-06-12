"""Add retrieval step timestamp index.

Revision ID: f9c0d1e2f3a4
Revises: f9b0c1d2e3f4
Create Date: 2026-06-12 08:35:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "f9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_retrieval_steps_created",
        "retrieval_steps",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_retrieval_steps_created", table_name="retrieval_steps")
