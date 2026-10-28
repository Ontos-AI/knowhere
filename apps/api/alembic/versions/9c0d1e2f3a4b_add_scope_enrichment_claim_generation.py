"""Fence scope enrichment publications across recovered worker claims."""

from alembic import op

revision: str = "9c0d1e2f3a4b"
down_revision: str = "8b9c0d1e2f3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE document_scope_enrichments ADD COLUMN IF NOT EXISTS claim_generation VARCHAR(36)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE document_scope_enrichments DROP COLUMN IF EXISTS claim_generation"
    )
