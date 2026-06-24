"""Rename document chunk ordinal to position.

Revision ID: fa0b1c2d3e4f
Revises: f9e0f1a2b3c4
Create Date: 2026-06-24 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fa0b1c2d3e4f"
down_revision: Union[str, Sequence[str], None] = "f9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("uq_document_chunks_revision_ordinal", table_name="document_chunks")
    op.alter_column(
        "document_chunks",
        "ordinal",
        new_column_name="position",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.create_index(
        "uq_document_chunks_revision_position",
        "document_chunks",
        ["document_id", "job_result_id", "position"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_document_chunks_revision_position", table_name="document_chunks")
    op.alter_column(
        "document_chunks",
        "position",
        new_column_name="ordinal",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.create_index(
        "uq_document_chunks_revision_ordinal",
        "document_chunks",
        ["document_id", "job_result_id", "ordinal"],
        unique=True,
    )
