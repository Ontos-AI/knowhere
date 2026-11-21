"""Drop the legacy knowledge_base and path_base tables.

Only the 2026-03-05 baseline creates them. Parsed content lives in the
revision-scoped document chunk, section and map-unit tables, and no code
reads or writes these two tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e108c52f6bf8"
down_revision: str | None = "b0132c4d5e6f"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_path_base_id", table_name="path_base")
    op.drop_table("path_base")
    op.drop_index("ix_knowledge_base_id", table_name="knowledge_base")
    op.drop_table("knowledge_base")


def downgrade() -> None:
    op.create_table(
        "knowledge_base",
        sa.Column(
            "id", sa.String(length=36), nullable=False, comment="Content row identifier"
        ),
        sa.Column("content", sa.Text(), nullable=True, comment="Raw content text"),
        sa.Column("path", sa.Text(), nullable=True, comment="File path or source"),
        sa.Column(
            "type",
            sa.String(length=2000),
            nullable=True,
            comment="Content type, for example PTXT or SUMMARY",
        ),
        sa.Column(
            "length", sa.Integer(), nullable=True, comment="Content length or size"
        ),
        sa.Column(
            "keywords", sa.String(length=511), nullable=True, comment="Content keywords"
        ),
        sa.Column("summary", sa.Text(), nullable=True, comment="Content summary"),
        sa.Column(
            "know_id",
            sa.String(length=128),
            nullable=True,
            comment="Knowledge record ID that may refer to an external knowledge base",
        ),
        sa.Column("tokens", sa.Text(), nullable=True, comment="Tokenized content"),
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=True,
            comment="Semantic vector used for similarity search",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_base_id", "knowledge_base", ["id"], unique=False)
    op.create_table(
        "path_base",
        sa.Column(
            "id", sa.String(length=36), nullable=False, comment="Path row identifier"
        ),
        sa.Column("path", sa.Text(), nullable=True, comment="File path or source"),
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=True,
            comment="Semantic vector used for similarity search",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_path_base_id", "path_base", ["id"], unique=False)
