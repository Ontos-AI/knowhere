"""Add dedicated shared demo tables and reserve their namespace."""

from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision: str = "4d5e6f7a8b9c"
down_revision: str = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS(SELECT 1 FROM documents WHERE namespace = '__knowhere_demo__')")).scalar_one():
        raise RuntimeError("Reserved demo namespace conflicts with existing private documents; stop and resolve the conflict before migration.")
    directory = Path(__file__).resolve().parent.parent
    # Frozen DDL is independent of future ORM changes. Alembic executes this
    # migration once; IF NOT EXISTS also tolerates pre-created indexes/tables.
    connection.exec_driver_sql((directory / "demo-corpus-schema.sql").read_text())
    connection.exec_driver_sql((directory / "demo-corpus-policies.sql").read_text())
    entries: list[dict[str, object]] = json.loads((directory / "demo-corpus-catalog.json").read_text())
    for entry in entries:
        connection.execute(sa.text("""
            INSERT INTO demo_documents
              (document_id, demo_source_id, title, category_id, catalog_metadata,
               user_id, namespace, status, source_file_name, parse_track, created_at, updated_at)
            VALUES (:document_id, :demo_source_id, :title, :category_id, CAST(:catalog_metadata AS json),
                    '__knowhere_demo__', '__knowhere_demo__', 'active', :file_name, 'chunk', now(), now())
            ON CONFLICT (demo_source_id) DO NOTHING
        """), {**entry, "catalog_metadata": json.dumps(entry["catalog_metadata"])})
    connection.exec_driver_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_demo_source
        ON jobs ((job_metadata ->> 'demo_source_id'))
        WHERE job_metadata ->> 'corpus_target' = 'DEMO'
          AND status IN ('waiting-file', 'pending', 'running', 'converting')
    """)


def downgrade() -> None:
    raise RuntimeError("Shared demo revisions must be retained. Roll back API/Worker images without dropping the additive schema.")
