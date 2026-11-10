"""Separate parsed revision availability from lexical publication."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "29d25dc0227b"
down_revision: str | None = "ad1e2f3a4b5c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _addColumns(table: str, columns: tuple[sa.Column, ...]) -> None:
    existing: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _addColumns("jobs", (
        sa.Column("parsing_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("indexing_status", sa.String(32), nullable=False, server_default="not_started"),
        sa.Column("failure_stage", sa.String(32), nullable=True),
        sa.Column("current_indexing_attempt_id", sa.String(36), nullable=True),
        sa.Column("webhook_events", sa.JSON(), nullable=True),
    ))
    op.execute("UPDATE jobs SET parsing_status='completed', indexing_status='completed' WHERE status='done'")
    op.execute("UPDATE jobs SET parsing_status='failed', failure_stage='parsing' WHERE status='failed' AND parsing_status IN ('pending', 'running') AND indexing_status='not_started'")
    for table in ("documents", "demo_documents"):
        _addColumns(table, (
            sa.Column("latest_parsed_job_result_id", sa.String(36), nullable=True),
            sa.Column("accepted_job_id", sa.String(36), nullable=True),
            sa.Column("current_indexing_build_id", sa.String(36), nullable=True),
        ))
        constraints: set[str] = {item["name"] for item in sa.inspect(op.get_bind()).get_foreign_keys(table) if item["name"] is not None}
        latestConstraint: str = f"fk_{table}_latest_parsed_revision"
        if latestConstraint not in constraints:
            op.create_foreign_key(latestConstraint, table, "job_results", ["latest_parsed_job_result_id"], ["id"], ondelete="RESTRICT" if table == "demo_documents" else "SET NULL")
        acceptedConstraint: str = f"fk_{table}_accepted_job"
        if acceptedConstraint not in constraints:
            op.create_foreign_key(acceptedConstraint, table, "jobs", ["accepted_job_id"], ["job_id"], deferrable=True, initially="DEFERRED")
        op.execute(sa.text(f"UPDATE {table} SET latest_parsed_job_result_id=current_job_result_id WHERE latest_parsed_job_result_id IS NULL"))
        op.execute(sa.text(f"UPDATE {table} d SET accepted_job_id=r.job_id FROM job_results r WHERE r.id=d.current_job_result_id AND d.accepted_job_id IS NULL"))
    if "document_indexing_attempts" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("document_indexing_attempts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False),
            sa.Column("revision_id", sa.String(36), sa.ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("execution_count", sa.Integer(), nullable=False),
            sa.Column("backend_epoch", sa.Integer(), nullable=False),
            sa.Column("current_build_id", sa.String(36), nullable=True),
            sa.Column("owner_token", sa.String(36), nullable=True),
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "attempt_number", name="uq_indexing_attempt_number"),
        )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_indexing_attempt ON document_indexing_attempts (job_id) WHERE state IN ('pending', 'running')")
    op.execute("CREATE INDEX IF NOT EXISTS idx_indexing_dispatch ON document_indexing_attempts (state, next_eligible_at)")


def downgrade() -> None:
    raise RuntimeError("Async publication state requires an explicit compatible backend rollback; retain additive schema")
