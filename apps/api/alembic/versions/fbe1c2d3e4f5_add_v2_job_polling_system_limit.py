"""Add v2 job polling system limit.

Revision ID: fbe1c2d3e4f5
Revises: fae1b2c3d4e5
Create Date: 2026-07-01 13:45:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "fbe1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "fae1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Cap v2 job-result polling like v1 job-result polling."""
    op.execute(
        """
        INSERT INTO system_limits (
            method,
            api_pattern,
            priority,
            rpm,
            period,
            description
        )
        VALUES (
            'GET',
            '/v2/jobs/*',
            200,
            200,
            'minute',
            'Job queries - prevent polling'
        )
        ON CONFLICT (method, api_pattern) DO NOTHING
        """
    )


def downgrade() -> None:
    """Remove the v2 job-result polling cap."""
    op.execute(
        """
        DELETE FROM system_limits
        WHERE method = 'GET'
          AND api_pattern = '/v2/jobs/*'
        """
    )
