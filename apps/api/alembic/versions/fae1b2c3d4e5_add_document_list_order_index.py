"""add document list order index

Revision ID: fae1b2c3d4e5
Revises: f9d0e1f2a3b4
Create Date: 2026-06-30 07:15:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "fae1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "f9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_user_namespace_status_updated
        ON documents (user_id, namespace, status, updated_at DESC, document_id ASC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_user_namespace_status_updated")
