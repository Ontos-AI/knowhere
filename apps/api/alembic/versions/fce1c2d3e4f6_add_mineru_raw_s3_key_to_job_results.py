"""Add mineru raw s3 key to job_results.

Revision ID: fce1c2d3e4f6
Revises: fbe1c2d3e4f5
Create Date: 2026-08-01 09:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "fce1c2d3e4f6"
down_revision: Union[str, Sequence[str], None] = "fbe1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_results",
        sa.Column("mineru_raw_s3_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_results", "mineru_raw_s3_key")
