"""add parse track to documents

Revision ID: f9b0c1d2e3f4
Revises: f9a0b1c2d3e4
Create Date: 2026-06-11 10:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "parse_track",
            sa.String(length=32),
            nullable=False,
            server_default="chunk",
        ),
    )
    op.alter_column("documents", "parse_track", server_default=None)


def downgrade() -> None:
    op.drop_column("documents", "parse_track")
