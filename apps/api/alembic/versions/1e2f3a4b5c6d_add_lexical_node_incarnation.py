"""Remember the native node incarnation whose index inventory was checked."""

from alembic import op

revision: str = "1e2f3a4b5c6d"
down_revision: str = "0d1e2f3a4b5c"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE lexical_backend_control ADD COLUMN IF NOT EXISTS node_incarnation VARCHAR(64)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE lexical_backend_control DROP COLUMN IF EXISTS node_incarnation"
    )
