"""add agentic workflow trace columns

Revision ID: f7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-14 03:20:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "retrieval_runs",
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("workflow_step_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("workflow_plan", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_retrieval_runs_parent_run_id",
        "retrieval_runs",
        ["parent_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_runs_parent_run_id", table_name="retrieval_runs")
    op.drop_column("retrieval_runs", "workflow_plan")
    op.drop_column("retrieval_runs", "workflow_step_id")
    op.drop_column("retrieval_runs", "parent_run_id")
