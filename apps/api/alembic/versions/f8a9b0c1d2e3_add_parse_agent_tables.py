"""add parse agent tables

Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2
Create Date: 2026-05-22 10:45:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parse_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="profile"),
        sa.Column("final_status", sa.String(length=32), nullable=False),
        sa.Column("rounds_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("idx_parse_runs_job_kind", "parse_runs", ["job_id", "kind"])
    op.create_index("idx_parse_runs_started", "parse_runs", ["started_at"])

    op.create_table(
        "parse_steps",
        sa.Column("step_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("tool_args", sa.JSON(), nullable=True),
        sa.Column("observation", sa.JSON(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["parse_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("step_id"),
    )
    op.create_index("idx_parse_steps_run_round", "parse_steps", ["run_id", "round_index"])
    op.create_index("idx_parse_steps_tool", "parse_steps", ["tool_name"])

    op.create_table(
        "document_page_plan",
        sa.Column("page_plan_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shard_plan", sa.JSON(), nullable=True),
        sa.Column("global_signals", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("page_plan_id"),
    )
    op.create_index("idx_document_page_plan_job", "document_page_plan", ["job_id"])
    op.create_index("idx_document_page_plan_created", "document_page_plan", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_document_page_plan_created", table_name="document_page_plan")
    op.drop_index("idx_document_page_plan_job", table_name="document_page_plan")
    op.drop_table("document_page_plan")
    op.drop_index("idx_parse_steps_tool", table_name="parse_steps")
    op.drop_index("idx_parse_steps_run_round", table_name="parse_steps")
    op.drop_table("parse_steps")
    op.drop_index("idx_parse_runs_started", table_name="parse_runs")
    op.drop_index("idx_parse_runs_job_kind", table_name="parse_runs")
    op.drop_table("parse_runs")
