"""Add GIN indexes for graph keyword and entity candidate filtering."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "2d3e4f5a6b72"
down_revision: str | None = "2c3d4e5f6a71"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_KEYWORD_INDEX = "idx_graph_nodes_top_keywords_gin"
_ENTITY_INDEX = "idx_graph_nodes_top_entities_gin"


def _create_indexes(*, concurrently: bool) -> None:
    concurrent_clause = "CONCURRENTLY " if concurrently else ""
    op.execute(
        f"CREATE INDEX {concurrent_clause}IF NOT EXISTS {_KEYWORD_INDEX} "
        "ON graph_nodes USING gin ((properties::jsonb -> 'top_keywords'))"
    )
    op.execute(
        f"CREATE INDEX {concurrent_clause}IF NOT EXISTS {_ENTITY_INDEX} "
        "ON graph_nodes USING gin ((properties::jsonb -> 'top_entities'))"
    )


def upgrade() -> None:
    uses_external_transaction = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if uses_external_transaction:
        _create_indexes(concurrently=False)
        return
    with op.get_context().autocommit_block():
        _create_indexes(concurrently=True)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_ENTITY_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_KEYWORD_INDEX}")
