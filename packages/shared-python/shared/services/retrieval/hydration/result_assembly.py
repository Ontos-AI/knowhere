from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts
from shared.services.retrieval.hydration.row_utils import (
    extract_page_nums,
    filter_excluded_rows,
    iter_connected_target_ids,
    normalize_chunk_type,
)


async def assemble_retrieval_results(
    *,
    db: AsyncSession | None = None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    document_scope: DocumentScope = DocumentScope(),
    allowed_chunk_types: set[str] | None = None,
    revision_pins: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    scoped_rows = filter_excluded_rows(
        rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        document_scope=document_scope,
    )
    hydrated_rows = await hydrate_connected_target_rows(
        db=db,
        rows=scoped_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        document_scope=document_scope,
        revision_pins=revision_pins,
    )
    rows_by_chunk_id = {
        str(row.get('chunk_id') or ''): row
        for row in [*scoped_rows, *hydrated_rows]
        if row.get('chunk_id')
    }
    filtered_rows = _filter_rows_by_allowed_chunk_types(
        scoped_rows,
        allowed_chunk_types=allowed_chunk_types,
        rows_by_chunk_id=rows_by_chunk_id,
    )

    embedded_targets: set[str] = set()
    for row in filtered_rows:
        for target_id in iter_connected_target_ids(row):
            if target_id in rows_by_chunk_id:
                embedded_targets.add(target_id)

    assembled: list[dict[str, Any]] = []
    for row in filtered_rows:
        if row.get('chunk_id') in embedded_targets:
            continue
        assembled_row = dict(row)
        base_content = str(row.get('content') or '')
        chunk_type = normalize_chunk_type(row.get('chunk_type'))
        if chunk_type == 'page':
            assembled_row['content'] = _page_summary(row)
            assembled_row['content_source'] = 'summary'
            page_nums = extract_page_nums(row)
            if page_nums is not None:
                assembled_row['page_nums'] = page_nums
        else:
            assembled_row['content'] = base_content
            assembled_row['content_source'] = 'content'
        assembled_row['composed'] = compose_evidence_parts(
            assembled_row,
            rows_by_chunk_id,
        )
        assembled.append(assembled_row)
    return assembled


def _filter_rows_by_allowed_chunk_types(
    rows: list[dict[str, Any]],
    *,
    allowed_chunk_types: set[str] | None,
    rows_by_chunk_id: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if allowed_chunk_types is None:
        return rows

    return [
        row
        for row in rows
        if normalize_chunk_type(row.get('chunk_type')) in allowed_chunk_types
        or any(
            normalize_chunk_type(rows_by_chunk_id.get(target_id, {}).get('chunk_type'))
            in allowed_chunk_types
            for target_id in iter_connected_target_ids(row)
        )
    ]


def _page_summary(row: dict[str, Any]) -> str:
    metadata = row.get('chunk_metadata') or row.get('metadata') or {}
    if not isinstance(metadata, dict):
        return ''
    return str(metadata.get('summary') or '').strip()


def _image_display_content(row: dict[str, Any]) -> str:
    display_ref = (
        str(row.get('asset_url') or '').strip()
        or str(row.get('file_path') or '').strip()
    )
    description = str(row.get('content') or '').strip()
    lines: list[str] = []
    if display_ref:
        lines.append(f'[Image: {display_ref}]')
    elif description:
        lines.append('[Image description]')
    if description:
        lines.extend(line for line in description.split('\n') if line.strip())
    return '\n'.join(lines)
