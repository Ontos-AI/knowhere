"""Add a covering index for persisted map-nav token lookups."""

from __future__ import annotations

from alembic import op


revision = "2e3f4a5b6c7d"
down_revision = "1d2e3f4a5b6c"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_document_map_unit_tokens_unit_lookup"


def upgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    statement = (
        f"CREATE INDEX {{concurrently}}IF NOT EXISTS {_INDEX_NAME} "
        "ON document_map_unit_tokens (map_unit_id, channel, token_hash) "
        "INCLUDE (token, frequency)"
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
    if external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
