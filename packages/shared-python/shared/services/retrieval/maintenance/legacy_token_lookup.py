"""Guarded retirement shared by the migration and the operator command."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

_LEGACY_INDEX: str = "idx_document_map_unit_tokens_lookup"
_DEFINITIONS: dict[str, str] = {
    _LEGACY_INDEX: "(channel, token_hash, map_unit_id)",
    "idx_document_map_unit_tokens_token_lookup_binary": (
        "(channel, decode((token_hash)::text, 'hex'::text)) "
        "INCLUDE (map_unit_id, token, frequency)"
    ),
    "idx_document_map_unit_tokens_unit_lookup": (
        "(map_unit_id, channel, token_hash) INCLUDE (token, frequency)"
    ),
}


class LegacyTokenLookupMaintenance:
    """Change only the legacy nonunique index after catalog validation.

    Concurrent operations require an AUTOCOMMIT connection. Transaction-owned
    migration contracts may explicitly use regular DDL. Definitions are checked
    conservatively so a same-name invalid, partial, or incompatible index cannot
    silently satisfy a prerequisite. Keep these historical definitions stable.
    """

    def __init__(self, connection: Connection, *, is_concurrent: bool = True) -> None:
        self._connection: Connection = connection
        self._is_concurrent: bool = is_concurrent

    def inspect_indexes(self) -> dict[str, dict[str, str | bool]]:
        rows = self._connection.execute(
            text(
                """
                SELECT relation.relname AS name, index.indisvalid AS is_valid,
                       index.indisready AS is_ready,
                       pg_get_indexdef(index.indexrelid) AS definition
                FROM pg_index AS index
                JOIN pg_class AS relation ON relation.oid = index.indexrelid
                WHERE index.indrelid = to_regclass('public.document_map_unit_tokens')
                ORDER BY relation.relname
                """
            )
        ).mappings().all()
        return {
            str(row["name"]): {
                "is_valid": bool(row["is_valid"]),
                "is_ready": bool(row["is_ready"]),
                "definition": str(row["definition"]),
            }
            for row in rows
        }

    def _require_compatible_index(
        self, indexes: dict[str, dict[str, str | bool]], name: str
    ) -> None:
        state: dict[str, str | bool] | None = indexes.get(name)
        expected: str = (
            f"CREATE INDEX {name} ON public.document_map_unit_tokens "
            f"USING btree {_DEFINITIONS[name]}"
        )
        if (
            state is None
            or not state["is_valid"]
            or not state["is_ready"]
            or state["definition"] != expected
        ):
            raise RuntimeError(f"Required token index is missing or incompatible: {name}")

    def apply_retirement(self) -> None:
        indexes: dict[str, dict[str, str | bool]] = self.inspect_indexes()
        for name in _DEFINITIONS:
            if name != _LEGACY_INDEX:
                self._require_compatible_index(indexes, name)
        if _LEGACY_INDEX not in indexes:
            return
        self._require_compatible_index(indexes, _LEGACY_INDEX)
        concurrent_clause: str = "CONCURRENTLY " if self._is_concurrent else ""
        self._connection.execute(
            text(f"DROP INDEX {concurrent_clause}IF EXISTS public.{_LEGACY_INDEX}")
        )
        if _LEGACY_INDEX in self.inspect_indexes():
            raise RuntimeError("Legacy token lookup index remained after retirement")

    def restore_index(self) -> None:
        indexes: dict[str, dict[str, str | bool]] = self.inspect_indexes()
        if _LEGACY_INDEX in indexes:
            self._require_compatible_index(indexes, _LEGACY_INDEX)
            return
        concurrent_clause: str = "CONCURRENTLY " if self._is_concurrent else ""
        self._connection.execute(
            text(
                f"CREATE INDEX {concurrent_clause}{_LEGACY_INDEX} "
                "ON public.document_map_unit_tokens USING btree "
                f"{_DEFINITIONS[_LEGACY_INDEX]}"
            )
        )
        self._require_compatible_index(self.inspect_indexes(), _LEGACY_INDEX)
