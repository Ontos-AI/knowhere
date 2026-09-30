"""Opt in to retiring the legacy token lookup after replacement validation."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op

from shared.services.retrieval.maintenance.legacy_token_lookup import (
    LegacyTokenLookupMaintenance,
)

revision: str = "3c4d5e6f7a8b"
down_revision: str | None = "2b3c4d5e6f7a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _run_maintenance(*, is_restore: bool) -> None:
    if context.is_offline_mode():
        raise RuntimeError("Legacy token index maintenance requires live catalog checks")
    is_external_transaction: bool = bool(
        op.get_context().opts.get("knowhere_external_transaction", False)
    )

    def execute() -> None:
        maintenance: LegacyTokenLookupMaintenance = LegacyTokenLookupMaintenance(
            op.get_bind(), is_concurrent=not is_external_transaction
        )
        if is_restore:
            maintenance.restore_index()
        else:
            maintenance.apply_retirement()

    if is_external_transaction:
        execute()
        return
    with op.get_context().autocommit_block():
        execute()


def upgrade() -> None:
    """Default deployment records the revision and retains the legacy index."""
    gate: str | None = context.get_x_argument(as_dictionary=True).get(
        "retire_legacy_token_lookup"
    )
    if gate is None or gate == "false":
        return
    if gate != "true":
        raise ValueError("retire_legacy_token_lookup must be exactly true or false")
    _run_maintenance(is_restore=False)


def downgrade() -> None:
    """Restore the exact legacy definition, including after operator retirement."""
    _run_maintenance(is_restore=True)
