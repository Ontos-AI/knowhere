"""Record the UTC days each caller searched each namespace.

Document importance ages by these days instead of calendar days, so documents
in a namespace nobody uses do not grow older. The backfill takes the days of
recorded retrieval runs. Days before those records, and days with only cache
hits or tool calls, are not restored; documents uploaded before then rank as
slightly fresher.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e71664cb1cee"
down_revision: str | None = "809ee432a5f6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_active_days",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.String(255), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
    )
    # Run times are stored as naive UTC, so their date is the UTC day.
    op.execute(
        "INSERT INTO retrieval_active_days (user_id, namespace, day) "
        "SELECT DISTINCT user_id, namespace, created_at::date FROM retrieval_runs "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("retrieval_active_days")
