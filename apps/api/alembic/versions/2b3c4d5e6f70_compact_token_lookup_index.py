"""Compact token lookup indexes without changing the retrieval query contract."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "2b3c4d5e6f70"
down_revision: str | None = "1a2b3c4d5e6f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_map_unit_tokens_token_lookup_compact"
_KEY_COLUMNS = "(channel, token_hash)"
_OLD_DEFINITION = "(channel, token_hash, map_unit_id)"
_OLD_INCLUDE = "INCLUDE (token, frequency)"
_COMPACT_DEFINITION = "(channel, token_hash)"
_COMPACT_INCLUDE = "INCLUDE (map_unit_id, token, frequency)"


def _index_definition(index_name: str) -> tuple[bool, bool, str] | None:
    row = op.get_bind().execute(
        text(
            "SELECT indexes.indisvalid, indexes.indisready, "
            "pg_get_indexdef(indexes.indexrelid) "
            "FROM pg_index AS indexes "
            "JOIN pg_class AS classes ON classes.oid = indexes.indexrelid "
            "JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace "
            "WHERE namespaces.nspname = current_schema() "
            "AND classes.relname = :index_name"
        ),
        {"index_name": index_name},
    ).one_or_none()
    if row is None:
        return None
    return bool(row[0]), bool(row[1]), str(row[2])


def _matches(index_name: str, *, compact: bool) -> bool:
    state = _index_definition(index_name)
    if state is None:
        return False
    is_valid, is_ready, definition = state
    if not is_valid or not is_ready:
        return False
    if compact:
        return _COMPACT_DEFINITION in definition and _COMPACT_INCLUDE in definition
    return _OLD_DEFINITION in definition and _OLD_INCLUDE in definition


def _ensure_index(*, compact: bool, concurrently: bool) -> None:
    concurrent_clause = "CONCURRENTLY " if concurrently else ""
    if _matches(_INDEX_NAME, compact=compact):
        return
    include_columns = (
        " INCLUDE (map_unit_id, token, frequency)"
        if compact
        else " INCLUDE (token, frequency)"
    )
    op.execute(
        f"CREATE INDEX {concurrent_clause}IF NOT EXISTS {_INDEX_NAME} "
        f"ON document_map_unit_tokens {_KEY_COLUMNS if compact else _OLD_DEFINITION}"
        f"{include_columns}"
    )


def _run_replacement(*, compact: bool) -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        _ensure_index(compact=compact, concurrently=False)
        return
    with op.get_context().autocommit_block():
        _ensure_index(compact=compact, concurrently=True)


def upgrade() -> None:
    """Narrow the lookup key while retaining index-only retrieval columns."""
    _run_replacement(compact=True)


def downgrade() -> None:
    """Remove only the additive compact lookup index."""
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
