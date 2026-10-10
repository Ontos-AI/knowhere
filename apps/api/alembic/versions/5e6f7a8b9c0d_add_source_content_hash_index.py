"""Index future source content fingerprints without backfilling existing Jobs."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "5e6f7a8b9c0d"
down_revision: str = "4d5e6f7a8b9c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _create_index(*, concurrently: bool) -> None:
    existing_index: tuple[bool, str, str] | None = (
        op.get_bind()
        .execute(
            text(
                "SELECT indexes.indisvalid AND indexes.indisready, "
                "tables.relname, pg_get_indexdef(indexes.indexrelid) "
                "FROM pg_index AS indexes "
                "JOIN pg_class AS classes ON classes.oid = indexes.indexrelid "
                "JOIN pg_class AS tables ON tables.oid = indexes.indrelid "
                "JOIN pg_namespace AS namespaces ON namespaces.oid = classes.relnamespace "
                "WHERE namespaces.nspname = current_schema() "
                "AND classes.relname = 'idx_jobs_user_source_content_hash'"
            )
        )
        .tuples()
        .first()
    )
    if existing_index is not None:
        is_ready, table_name, definition = existing_index
        normalized_definition: str = " ".join(definition.split())
        intended_definition: str = (
            "USING btree (user_id, ((job_metadata ->> 'source_content_sha256'::text))) "
            "WHERE ((job_metadata ->> 'source_content_sha256'::text) IS NOT NULL)"
        )
        if (
            is_ready
            and table_name == "jobs"
            and normalized_definition.startswith("CREATE INDEX ")
            and normalized_definition.endswith(intended_definition)
        ):
            return
    concurrent_clause: str = "CONCURRENTLY " if concurrently else ""
    op.execute(
        f"DROP INDEX {concurrent_clause}IF EXISTS idx_jobs_user_source_content_hash"
    )
    op.execute(
        f"CREATE INDEX {concurrent_clause}IF NOT EXISTS idx_jobs_user_source_content_hash "
        "ON jobs (user_id, (job_metadata ->> 'source_content_sha256')) "
        "WHERE job_metadata ->> 'source_content_sha256' IS NOT NULL"
    )


def upgrade() -> None:
    uses_external_transaction: bool = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )
    if uses_external_transaction:
        _create_index(concurrently=False)
        return
    with op.get_context().autocommit_block():
        _create_index(concurrently=True)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_jobs_user_source_content_hash")
