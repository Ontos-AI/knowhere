"""Add trigram acceleration for regex searches over published chunk content."""

from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "0a1b2c3d4e5f"
down_revision: str | None = "9f0a1b2c3d4e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_chunks_content_trgm"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "
            "ON document_chunks USING gin (content gin_trgm_ops) "
            "WHERE content IS NOT NULL"
        )
        return
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON document_chunks USING gin (content gin_trgm_ops) "
            "WHERE content IS NOT NULL"
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
