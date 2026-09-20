"""Drop the content trigram index — grep now uses idx_document_chunks_term_trgm."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | Sequence[str] | None = "0b1c2d3e4f5a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "idx_document_chunks_content_trgm"


def upgrade() -> None:
    external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if external_transaction:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")


def downgrade() -> None:
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
