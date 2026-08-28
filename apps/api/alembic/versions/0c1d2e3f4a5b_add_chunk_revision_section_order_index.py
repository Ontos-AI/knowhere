"""Add the index used by lazy map-nav section loads."""

from __future__ import annotations

from alembic import op


revision = "0c1d2e3f4a5b"
down_revision = "fbf0c1d2e3f4"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_document_chunks_revision_section_order"


def upgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "
            "ON document_chunks "
            "(document_id, job_result_id, section_id, sort_order, chunk_id, id)"
        )
        return
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON document_chunks "
            "(document_id, job_result_id, section_id, sort_order, chunk_id, id)"
        )


def downgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
