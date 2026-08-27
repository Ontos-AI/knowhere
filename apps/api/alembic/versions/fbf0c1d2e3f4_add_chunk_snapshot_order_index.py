"""Add the chunk snapshot pagination order index.

Revision ID: fbf0c1d2e3f4
Revises: f0d85d209e68, fbe1c2d3e4f5
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "fbf0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = (
    "f0d85d209e68",
    "fbe1c2d3e4f5",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "idx_document_chunks_revision_snapshot_order"
_INDEX_COLUMNS = "(document_id, job_result_id, sort_order, chunk_id, id)"
_INDEX_METHOD_AND_COLUMNS = f"using btree {_INDEX_COLUMNS}"


def _read_index_definition() -> tuple[str, bool] | None:
    row = op.get_bind().execute(
        text(
            """
            SELECT indexdef, index_metadata.indisvalid
            FROM pg_indexes AS indexes
            JOIN pg_class AS index_class
              ON index_class.relname = indexes.indexname
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_class.relnamespace
             AND index_namespace.nspname = indexes.schemaname
            JOIN pg_index AS index_metadata
              ON index_metadata.indexrelid = index_class.oid
            WHERE indexes.schemaname = current_schema()
              AND indexes.tablename = 'document_chunks'
              AND indexes.indexname = :index_name
            """
        ),
        {"index_name": _INDEX_NAME},
    ).first()
    if row is None:
        return None
    return str(row[0]), bool(row[1])


def _index_is_intended() -> bool:
    definition = _read_index_definition()
    if definition is None or not definition[1]:
        return False
    normalized_definition = " ".join(definition[0].lower().split())
    return (
        normalized_definition.startswith("create index ")
        and _INDEX_METHOD_AND_COLUMNS in normalized_definition
        and " where " not in normalized_definition
    )


def _drop_index(*, concurrently: bool) -> None:
    concurrent_clause = "CONCURRENTLY " if concurrently else ""
    op.execute(f"DROP INDEX {concurrent_clause}IF EXISTS {_INDEX_NAME}")


def _create_index(*, concurrently: bool) -> None:
    concurrent_clause = "CONCURRENTLY " if concurrently else ""
    op.execute(
        f"""
        CREATE INDEX {concurrent_clause}IF NOT EXISTS {_INDEX_NAME}
        ON document_chunks {_INDEX_COLUMNS}
        """
    )


def upgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        if _read_index_definition() is not None and not _index_is_intended():
            _drop_index(concurrently=False)
        if _read_index_definition() is None:
            _create_index(concurrently=False)
        return

    # Index creation must not hold a write lock on document_chunks while the
    # production corpus is being indexed.  CONCURRENTLY cannot run inside the
    # transaction Alembic normally opens, so switch to an autocommit block.
    with op.get_context().autocommit_block():
        if _read_index_definition() is not None and not _index_is_intended():
            _drop_index(concurrently=True)
        if _read_index_definition() is None:
            _create_index(concurrently=True)


def downgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        _drop_index(concurrently=False)
    else:
        with op.get_context().autocommit_block():
            _drop_index(concurrently=True)
