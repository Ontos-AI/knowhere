"""Use a binary expression key for the token lookup index."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "2e3f4a5b6c73"
down_revision: str | None = "2d3e4f5a6b72"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_map_unit_tokens_token_lookup_binary"
_KEY_COLUMNS = "(channel, (decode(token_hash, 'hex'::text)))"
_INCLUDE_COLUMNS = " INCLUDE (map_unit_id, token, frequency)"


def _run_index_creation() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    concurrent_clause = "" if external_transaction else "CONCURRENTLY "
    if external_transaction:
        _create_index(concurrent_clause=concurrent_clause)
        return
    with op.get_context().autocommit_block():
        _create_index(concurrent_clause=concurrent_clause)


def _create_index(*, concurrent_clause: str) -> None:
    op.execute(
        f"CREATE INDEX {concurrent_clause}IF NOT EXISTS {_INDEX_NAME} "
        f"ON document_map_unit_tokens {_KEY_COLUMNS}"
        f"{_INCLUDE_COLUMNS}"
    )


def upgrade() -> None:
    """Add a binary token-hash lookup index without replacing live indexes."""
    _run_index_creation()


def downgrade() -> None:
    """Remove only the additive binary token-hash lookup index."""
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
