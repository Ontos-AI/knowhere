from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.reference import hydrate_referenced_chunk_rows
from shared.services.retrieval.execution.response_projection import (
    enrich_referenced_chunks_with_asset_urls,
)
from shared.services.retrieval.hydration.row_utils import build_reference_lookup_key


@dataclass(frozen=True)
class ResolvedWorkflowReferences:
    refs: list[dict[str, Any]]
    rows: list[dict[str, Any]]


async def resolve_workflow_references(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    refs: list[dict[str, Any]],
) -> ResolvedWorkflowReferences:
    enriched_refs = await enrich_referenced_chunks_with_asset_urls(refs)
    hydrated_rows = await hydrate_referenced_chunk_rows(
        db=db,
        user_id=user_id,
        namespace=namespace,
        refs=enriched_refs,
    )
    return _select_matching_references(enriched_refs, hydrated_rows)


def _select_matching_references(
    refs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> ResolvedWorkflowReferences:
    selected_refs: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    seen_row_keys: set[tuple[str, str, str, str]] = set()

    for ref in refs:
        matching_row = next(
            (
                row
                for row in rows
                if _matches_reference(ref, row)
                and _row_key(row) not in seen_row_keys
            ),
            None,
        )
        if matching_row is None:
            continue

        selected_refs.append(ref)
        selected_rows.append(matching_row)
        seen_row_keys.add(_row_key(matching_row))

    return ResolvedWorkflowReferences(refs=selected_refs, rows=selected_rows)


def _matches_reference(ref: dict[str, Any], row: dict[str, Any]) -> bool:
    ref_key = build_reference_lookup_key(
        document_id=ref.get("document_id"),
        chunk_id=ref.get("chunk_id"),
        section_path=ref.get("section_path"),
        file_path=ref.get("file_path"),
    )
    row_key = _row_key(row)
    if ref_key[:2] != row_key[:2]:
        return False
    if ref_key[2] and ref_key[2] != row_key[2] and not _matches_root_alias(ref, row):
        return False
    if ref_key[3] and ref_key[3] != row_key[3]:
        return False
    return True


def _row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return build_reference_lookup_key(
        document_id=row.get("document_id"),
        chunk_id=row.get("chunk_id"),
        section_path=row.get("section_path"),
        file_path=row.get("file_path"),
    )


def _matches_root_alias(ref: dict[str, Any], row: dict[str, Any]) -> bool:
    ref_section_path = str(ref.get("section_path") or "").strip()
    row_section_path = str(row.get("section_path") or "").strip()
    source_file_name = str(row.get("source_file_name") or "").strip()
    return bool(
        source_file_name
        and row_section_path == "Root"
        and ref_section_path == source_file_name
    )
