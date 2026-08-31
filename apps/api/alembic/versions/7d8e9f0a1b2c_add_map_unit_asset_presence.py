"""Add has_image/has_table presence flags to document_map_units."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7d8e9f0a1b2c"
down_revision = "6c7d8e9f0a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("document_map_units")}
    if "has_image" not in columns:
        op.add_column(
            "document_map_units",
            sa.Column(
                "has_image", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    if "has_table" not in columns:
        op.add_column(
            "document_map_units",
            sa.Column(
                "has_table", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )

    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("document_map_units")}
    if "idx_document_map_units_has_image" not in indexes:
        op.create_index(
            "idx_document_map_units_has_image",
            "document_map_units",
            ["document_id", "job_result_id"],
            postgresql_where=sa.text("has_image = true"),
        )
    if "idx_document_map_units_has_table" not in indexes:
        op.create_index(
            "idx_document_map_units_has_table",
            "document_map_units",
            ["document_id", "job_result_id"],
            postgresql_where=sa.text("has_table = true"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("document_map_units")}
    if "idx_document_map_units_has_table" in indexes:
        op.drop_index("idx_document_map_units_has_table", table_name="document_map_units")
    if "idx_document_map_units_has_image" in indexes:
        op.drop_index("idx_document_map_units_has_image", table_name="document_map_units")

    columns = {col["name"] for col in inspector.get_columns("document_map_units")}
    if "has_table" in columns:
        op.drop_column("document_map_units", "has_table")
    if "has_image" in columns:
        op.drop_column("document_map_units", "has_image")
