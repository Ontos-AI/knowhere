"""Persist concrete native identity, execution builds, and independent leases."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d74fe2bd581c"
down_revision: str | None = "29d25dc0227b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    tables: set[str] = set(sa.inspect(op.get_bind()).get_table_names())
    columns: set[str] = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("job_results")}
    for name, length in (("indexing_input_key", 512), ("indexing_input_checksum", 64), ("indexing_input_compiler", 64)):
        if name not in columns:
            op.add_column("job_results", sa.Column(name, sa.String(length), nullable=True))
    if "lexical_backend_control" not in tables:
        op.create_table("lexical_backend_control",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("backend", sa.String(32), nullable=False),
            sa.Column("mode", sa.String(32), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("cluster_uuid", sa.String(64), nullable=True),
        )
    if "lexical_namespace_indexes" not in tables:
        op.create_table("lexical_namespace_indexes",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("namespace", sa.String(255), nullable=False),
            sa.Column("corpus_kind", sa.String(16), nullable=False),
            sa.Column("index_name", sa.String(128), nullable=False, unique=True),
            sa.Column("index_uuid", sa.String(64), nullable=True),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("operation_id", sa.String(36), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("corpus_kind", "owner_id", "namespace", name="uq_lexical_namespace_scope"),
        )
    if "document_indexing_builds" not in tables:
        op.create_table("document_indexing_builds",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("attempt_id", sa.String(36), sa.ForeignKey("document_indexing_attempts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("namespace_index_id", sa.String(64), sa.ForeignKey("lexical_namespace_indexes.id"), nullable=False),
            sa.Column("document_id", sa.String(36), nullable=False),
            sa.Column("revision_id", sa.String(36), sa.ForeignKey("job_results.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_token", sa.String(36), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("unit_count", sa.Integer(), nullable=False),
            sa.Column("activation_generation", sa.Integer(), nullable=True),
            sa.Column("retirement_generation", sa.Integer(), nullable=True),
            sa.Column("cleanup_state", sa.String(32), nullable=False),
            sa.Column("legacy_copy_state", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "lexical_operation_leases" not in tables:
        op.create_table("lexical_operation_leases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("namespace_index_id", sa.String(64), sa.ForeignKey("lexical_namespace_indexes.id"), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("owner_token", sa.String(36), nullable=False),
            sa.Column("backend_epoch", sa.Integer(), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=True),
            sa.Column("build_id", sa.String(36), nullable=True),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_lexical_build_scope ON document_indexing_builds (namespace_index_id, state)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_lexical_live_leases ON lexical_operation_leases (namespace_index_id, kind, state, expires_at)")


def downgrade() -> None:
    raise RuntimeError("Retain lexical coordination metadata for compatible backend rollback")
