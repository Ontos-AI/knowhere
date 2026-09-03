"""Repair a missing or invalid token-leading map-unit covering index."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_map_unit_tokens_token_lookup"
_INDEX_COLUMNS = "(channel, token_hash, map_unit_id) INCLUDE (token, frequency)"


def _is_index_ready() -> bool:
    """Return whether the current schema contains a usable covering index."""
    is_ready = op.get_bind().execute(
        text(
            "SELECT indexes.indisvalid AND indexes.indisready "
            "FROM pg_index AS indexes "
            "JOIN pg_class AS classes ON classes.oid = indexes.indexrelid "
            "JOIN pg_namespace AS namespaces "
            "ON namespaces.oid = classes.relnamespace "
            "WHERE namespaces.nspname = current_schema() "
            "AND classes.relname = :index_name"
        ),
        {"index_name": _INDEX_NAME},
    ).scalar_one_or_none()
    return bool(is_ready)


def _repair_index(*, concurrently: bool) -> None:
    """Replace a missing or unusable index using the allowed DDL mode."""
    if _is_index_ready():
        return
    concurrent_clause: str = "CONCURRENTLY " if concurrently else ""
    op.execute(f"DROP INDEX {concurrent_clause}IF EXISTS {_INDEX_NAME}")
    op.execute(
        f"CREATE INDEX {concurrent_clause}{_INDEX_NAME} "
        f"ON document_map_unit_tokens {_INDEX_COLUMNS}"
    )


def upgrade() -> None:
    """Ensure the additive covering index exists and is usable."""
    uses_external_transaction: bool = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if uses_external_transaction:
        _repair_index(concurrently=False)
        return
    with op.get_context().autocommit_block():
        _repair_index(concurrently=True)


def downgrade() -> None:
    """Keep the index owned by the preceding additive migration."""
