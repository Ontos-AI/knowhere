"""Add revision-pinned map-nav score units."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "1d2e3f4a5b6c"
down_revision = "0c1d2e3f4a5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("document_map_unit_indexes"):
        op.create_table(
            "document_map_unit_indexes",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("job_result_id", sa.String(length=36), nullable=False),
            sa.Column("format_version", sa.Integer(), nullable=False),
            sa.Column("unit_count", sa.Integer(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.document_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["job_result_id"], ["job_results.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "document_id",
                "job_result_id",
                name="uq_document_map_unit_indexes_revision",
            ),
        )
    if not inspector.has_table("document_map_units"):
        op.create_table(
            "document_map_units",
            sa.Column("id", sa.String(length=160), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("job_result_id", sa.String(length=36), nullable=False),
            sa.Column("unit_id", sa.String(length=128), nullable=False),
            sa.Column("section_id", sa.String(length=36), nullable=False),
            sa.Column("unit_kind", sa.String(length=32), nullable=False),
            sa.Column("path_token_count", sa.Integer(), nullable=False),
            sa.Column("content_token_count", sa.Integer(), nullable=False),
            sa.Column("term_search_text_lower", sa.Text(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.document_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["job_result_id"], ["job_results.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    if not inspector.has_table("document_map_unit_tokens"):
        op.create_table(
            "document_map_unit_tokens",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("map_unit_id", sa.String(length=160), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=False),
            sa.Column("token", sa.Text(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("frequency", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["map_unit_id"], ["document_map_units.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(bind)
    indexes = {
        item["name"] for item in inspector.get_indexes("document_map_unit_indexes")
    }
    if "idx_document_map_unit_indexes_revision" not in indexes:
        op.create_index(
            "idx_document_map_unit_indexes_revision",
            "document_map_unit_indexes",
            ["document_id", "job_result_id"],
        )
    indexes = {item["name"] for item in inspector.get_indexes("document_map_units")}
    if "idx_document_map_units_revision_order" not in indexes:
        op.create_index(
            "idx_document_map_units_revision_order",
            "document_map_units",
            ["document_id", "job_result_id", "sort_order", "unit_id"],
        )
    if "idx_document_map_units_section" not in indexes:
        op.create_index(
            "idx_document_map_units_section", "document_map_units", ["section_id"]
        )
    indexes = {
        item["name"] for item in inspector.get_indexes("document_map_unit_tokens")
    }
    if "idx_document_map_unit_tokens_lookup" not in indexes:
        op.create_index(
            "idx_document_map_unit_tokens_lookup",
            "document_map_unit_tokens",
            ["channel", "token_hash", "map_unit_id"],
        )
    if "idx_document_map_unit_tokens_unit" not in indexes:
        op.create_index(
            "idx_document_map_unit_tokens_unit",
            "document_map_unit_tokens",
            ["map_unit_id", "channel"],
        )


def downgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in (
        "document_map_unit_tokens",
        "document_map_units",
        "document_map_unit_indexes",
    ):
        if table_name in existing_tables:
            op.drop_table(table_name)
