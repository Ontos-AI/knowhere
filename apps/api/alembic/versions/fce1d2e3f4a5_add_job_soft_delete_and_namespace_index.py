"""Add job soft deletion and namespace listing index.

Revision ID: fce1d2e3f4a5
Revises: fbe1c2d3e4f5
Create Date: 2026-07-27 10:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "fce1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "fbe1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "deleted_at",
            sa.DateTime(),
            nullable=True,
            comment="Soft-deletion time; NULL when active",
        ),
    )
    op.create_index(
        "idx_job_user_active_created_at",
        "jobs",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_job_user_namespace",
        "jobs",
        ["user_id", sa.text("(job_metadata ->> 'namespace')")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_job_user_namespace", table_name="jobs")
    op.drop_index("idx_job_user_active_created_at", table_name="jobs")
    op.drop_column("jobs", "deleted_at")
