"""Replace retrieval_runs.data_type integer with chunk_types JSON.

Revision ID: a1b2c3d4e5f6
Revises: f9c0d1e2f3a4
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'f9c0d1e2f3a4'
branch_labels = None
depends_on = None

DATA_TYPE_TO_CHUNK_TYPES = {
    1: None,
    2: '["text"]',
    3: '["image"]',
    4: '["table"]',
    5: '["image", "text"]',
    6: '["table", "text"]',
    7: '["page"]',
    8: '["image", "table", "text"]',
}


def upgrade() -> None:
    op.add_column('retrieval_runs', sa.Column('chunk_types', sa.JSON, nullable=True))

    for data_type, json_val in DATA_TYPE_TO_CHUNK_TYPES.items():
        if json_val is None:
            op.execute(
                f"UPDATE retrieval_runs SET chunk_types = NULL WHERE data_type = {data_type}"
            )
        else:
            op.execute(
                f"UPDATE retrieval_runs SET chunk_types = '{json_val}' WHERE data_type = {data_type}"
            )

    op.drop_column('retrieval_runs', 'data_type')


def downgrade() -> None:
    op.add_column(
        'retrieval_runs',
        sa.Column('data_type', sa.Integer, nullable=False, server_default='1'),
    )

    op.execute("UPDATE retrieval_runs SET data_type = 1 WHERE chunk_types IS NULL")
    op.execute("UPDATE retrieval_runs SET data_type = 2 WHERE chunk_types = '[\"text\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 3 WHERE chunk_types = '[\"image\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 4 WHERE chunk_types = '[\"table\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 5 WHERE chunk_types = '[\"image\", \"text\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 6 WHERE chunk_types = '[\"table\", \"text\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 7 WHERE chunk_types = '[\"page\"]'")
    op.execute("UPDATE retrieval_runs SET data_type = 8 WHERE chunk_types = '[\"image\", \"table\", \"text\"]'")

    op.drop_column('retrieval_runs', 'chunk_types')
