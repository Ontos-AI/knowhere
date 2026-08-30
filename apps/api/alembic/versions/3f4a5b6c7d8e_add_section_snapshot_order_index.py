"""Add the index used by lazy map-nav section pagination."""

from __future__ import annotations

from alembic import op


revision = "3f4a5b6c7d8e"
down_revision = "2e3f4a5b6c7d"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_document_sections_revision_snapshot_order"


def upgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    statement = (
        f"CREATE INDEX {{concurrently}}IF NOT EXISTS {_INDEX_NAME} "
        "ON document_sections "
        "(document_id, job_result_id, sort_order, section_id)"
    )
    if external_transaction:
        op.execute(statement.format(concurrently=""))
        return
    with op.get_context().autocommit_block():
        op.execute(statement.format(concurrently="CONCURRENTLY "))


def downgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    statement = f"DROP INDEX {{concurrently}}IF EXISTS {_INDEX_NAME}"
    if external_transaction:
        op.execute(statement.format(concurrently=""))
        return
    with op.get_context().autocommit_block():
        op.execute(statement.format(concurrently="CONCURRENTLY "))
