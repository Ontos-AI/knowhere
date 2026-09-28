"""Retire token indexes that duplicate the serving lookup path."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "2f3g4h5i6j7"
down_revision: str | None = "2e3f4a5b6c73"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_RETIRED_INDEXES: tuple[str, ...] = (
    "idx_document_map_unit_tokens_token_lookup",
    "idx_document_map_unit_tokens_token_lookup_compact",
)


def _concurrent_clause() -> str:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    return "" if external_transaction else "CONCURRENTLY "


def upgrade() -> None:
    """Remove duplicate write-maintenance paths after candidate indexes exist."""
    concurrent_clause = _concurrent_clause()
    if concurrent_clause:
        with op.get_context().autocommit_block():
            for index_name in _RETIRED_INDEXES:
                op.execute(
                    f"DROP INDEX {concurrent_clause}IF EXISTS {index_name}"
                )
        return
    for index_name in _RETIRED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")


def downgrade() -> None:
    """Restore the retired indexes for rollback to the additive schema."""
    statements: tuple[str, ...] = (
        "CREATE INDEX IF NOT EXISTS idx_document_map_unit_tokens_token_lookup "
        "ON document_map_unit_tokens (channel, token_hash, map_unit_id) "
        "INCLUDE (token, frequency)",
        "CREATE INDEX IF NOT EXISTS idx_document_map_unit_tokens_token_lookup_compact "
        "ON document_map_unit_tokens (channel, token_hash) "
        "INCLUDE (map_unit_id, token, frequency)",
    )
    concurrent_clause = _concurrent_clause()
    if concurrent_clause:
        with op.get_context().autocommit_block():
            for statement in statements:
                op.execute(statement.replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY"))
        return
    for statement in statements:
        op.execute(statement)
