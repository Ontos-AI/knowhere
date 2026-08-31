"""Add average_idf columns to document_map_unit_indexes.

Stores the rank_bm25 Okapi average IDF per channel at index-write time so
query scoring never scans all tokens to rebuild it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8e9f0a1b2c3d"
down_revision = "7d8e9f0a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        col["name"] for col in inspector.get_columns("document_map_unit_indexes")
    }
    if "average_idf_path" not in columns:
        op.add_column(
            "document_map_unit_indexes",
            sa.Column(
                "average_idf_path",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
        )
    if "average_idf_content" not in columns:
        op.add_column(
            "document_map_unit_indexes",
            sa.Column(
                "average_idf_content",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        col["name"] for col in inspector.get_columns("document_map_unit_indexes")
    }
    if "average_idf_content" in columns:
        op.drop_column("document_map_unit_indexes", "average_idf_content")
    if "average_idf_path" in columns:
        op.drop_column("document_map_unit_indexes", "average_idf_path")
