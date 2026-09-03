"""Add the token-leading covering index for map-unit lookups."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "9f0a1b2c3d4e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_map_unit_tokens_token_lookup"


def upgrade() -> None:
    """Create the additive index without taking a table-wide write lock."""
    uses_external_transaction: bool = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    statement: str = (
        f"CREATE INDEX {{concurrently}}IF NOT EXISTS {_INDEX_NAME} "
        "ON document_map_unit_tokens (channel, token_hash, map_unit_id) "
        "INCLUDE (token, frequency)"
    )
    if uses_external_transaction:
        op.execute(statement.format(concurrently=""))
        return
    with op.get_context().autocommit_block():
        op.execute(statement.format(concurrently="CONCURRENTLY "))


def downgrade() -> None:
    """Remove only the index introduced by this migration."""
    uses_external_transaction: bool = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if uses_external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
