"""Let scope enrichments describe page ranges or spreadsheet sheet groups.

Revision ID: 9e0f1a2b3c4d
Revises: 8f901a2b3c4d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e0f1a2b3c4d"
down_revision: str | None = "8f901a2b3c4d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "document_scope_enrichments"
_KIND_PAYLOAD_CHECK = "ck_document_scope_enrichments_kind_payload"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "scope_kind",
            sa.String(length=32),
            nullable=False,
            server_default="page_range",
        ),
    )
    op.add_column(_TABLE, sa.Column("sheet_names", sa.JSON(), nullable=True))
    op.alter_column(_TABLE, "page_start", existing_type=sa.Integer(), nullable=True)
    op.alter_column(_TABLE, "page_end", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint(
        _KIND_PAYLOAD_CHECK,
        _TABLE,
        "(scope_kind = 'page_range'"
        " AND page_start IS NOT NULL AND page_end IS NOT NULL"
        " AND sheet_names IS NULL)"
        " OR (scope_kind = 'sheet_group'"
        " AND page_start IS NULL AND page_end IS NULL"
        " AND sheet_names IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE} WHERE scope_kind <> 'page_range'")
    op.drop_constraint(_KIND_PAYLOAD_CHECK, _TABLE, type_="check")
    op.alter_column(_TABLE, "page_end", existing_type=sa.Integer(), nullable=False)
    op.alter_column(_TABLE, "page_start", existing_type=sa.Integer(), nullable=False)
    op.drop_column(_TABLE, "sheet_names")
    op.drop_column(_TABLE, "scope_kind")
