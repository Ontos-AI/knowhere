"""resize page embeddings to 3072

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-10-26 21:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8b9c0d1e2f3a"
down_revision: str = "7a8b9c0d1e2f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = (
    "document_page_embeddings",
    "demo_document_page_embeddings",
)


def _resize_embedding_column(table_name: str, *, dim: int) -> None:
    index_name = f"idx_{table_name}_hnsw_cosine"
    op.drop_index(index_name, table_name=table_name)
    op.execute(f"DELETE FROM {table_name}")
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN embedding TYPE vector({int(dim)})")
    op.create_index(
        index_name,
        table_name,
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def upgrade() -> None:
    for table_name in _TABLES:
        _resize_embedding_column(table_name, dim=3072)


def downgrade() -> None:
    for table_name in reversed(_TABLES):
        _resize_embedding_column(table_name, dim=1024)
