"""Index the bounded enhancement recovery queue without indexing completed work."""

from alembic import op

revision: str = "ad1e2f3a4b5c"
down_revision: str = "9c0d1e2f3a4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().opts.get("knowhere_external_transaction", False):
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_results_pending_enhancement "
            "ON job_results (updated_at, id) "
            "WHERE (document_metadata->'progressive'->'enhancement'->>'status') IN ('pending', 'running')"
        )
        return
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_job_results_pending_enhancement "
            "ON job_results (updated_at, id) "
            "WHERE (document_metadata->'progressive'->'enhancement'->>'status') IN ('pending', 'running')"
        )


def downgrade() -> None:
    if op.get_context().opts.get("knowhere_external_transaction", False):
        op.execute("DROP INDEX IF EXISTS idx_job_results_pending_enhancement")
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_job_results_pending_enhancement")
