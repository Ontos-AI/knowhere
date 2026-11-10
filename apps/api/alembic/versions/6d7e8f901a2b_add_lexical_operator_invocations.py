"""Permanently consume operator invocation tokens across issuer replacement."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "6d7e8f901a2b"
down_revision: str | None = "5c6d7e8f901a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("lexical_operator_invocations"):
        op.create_table("lexical_operator_invocations",
            sa.Column("invocation_token", sa.String(36), primary_key=True),
            sa.Column("logical_cluster_id", sa.String(128), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["logical_cluster_id"], ["lexical_operator_issuers.logical_cluster_id"],
                name="fk_lexical_invocation_issuer"))
    op.execute("""INSERT INTO lexical_operator_invocations (invocation_token, logical_cluster_id, recorded_at)
        SELECT invocation_token::uuid::text, logical_cluster_id, acquired_at FROM lexical_operator_issuers
        ON CONFLICT (invocation_token) DO NOTHING""")
    op.execute("""CREATE OR REPLACE FUNCTION reject_lexical_invocation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Operator invocation ledger is append-only'; END $$""")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='lexical_operator_invocations'::regclass
            AND tgname='protect_lexical_invocation_rows') THEN
            CREATE TRIGGER protect_lexical_invocation_rows BEFORE UPDATE OR DELETE ON lexical_operator_invocations
                FOR EACH ROW EXECUTE FUNCTION reject_lexical_invocation_mutation();
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='lexical_operator_invocations'::regclass
            AND tgname='protect_lexical_invocation_truncate') THEN
            CREATE TRIGGER protect_lexical_invocation_truncate BEFORE TRUNCATE ON lexical_operator_invocations
                FOR EACH STATEMENT EXECUTE FUNCTION reject_lexical_invocation_mutation();
        END IF;
    END $$""")


def downgrade() -> None:
    raise RuntimeError("Retain permanently consumed operator invocation tokens across compatible rollback")
