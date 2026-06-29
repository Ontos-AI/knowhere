from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.row_utils import (
    clean_content,
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
    allowed_chunk_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    filtered_rows = filter_excluded_rows(
        rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    if allowed_chunk_types is not None:
        filtered_rows = [
            row for row in filtered_rows
            if normalize_chunk_type(row.get('chunk_type')) in allowed_chunk_types
        ]
    hydrated_rows = await hydrate_connected_target_rows(
        db=db,
        rows=filtered_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    rows_by_chunk_id = {
        str(row.get('chunk_id') or ''): row
        for row in [*filtered_rows, *hydrated_rows]
        if row.get('chunk_id')
    }

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
        elif chunk_type == 'table':
            assembled_row['content'] = _table_summary_content(row)
        elif chunk_type == 'text':
            connected_targets: list[tuple[int, str]] = []
            for target_id in iter_connected_target_ids(row):
                target_row = rows_by_chunk_id.get(target_id)
                if not target_row:
                    continue
                if normalize_chunk_type(target_row.get('chunk_type')) != 'table':
                    continue
                target_content = _table_summary_content(target_row)
                if target_content:
                    sort_key = int(target_row.get('sort_order', 0) or 0)
                    connected_targets.append((sort_key, target_content))
            connected_targets.sort(key=lambda item: item[0])
            related_parts = [content for _, content in connected_targets]

            # TODO: Dedicated Large Table Agent
            # For the Notebook/Agent environment, consider introducing a dedicated 
            # "Large Table Agent" that can fetch and query oversized tables via URL.
            if base_content and related_parts:
                assembled_row['content'] = '\n\n'.join([base_content, *related_parts])
            else:
                assembled_row['content'] = base_content
        else:
            assembled_row['content'] = base_content
        assembled_row['content'] = clean_content(assembled_row['content'])
        assembled.append(assembled_row)
    return assembled


def _page_summary(row: dict[str, Any]) -> str:
    metadata = row.get('chunk_metadata') or row.get('metadata') or {}
    if not isinstance(metadata, dict):
        return ''
    return str(metadata.get('summary') or '').strip()


def _table_summary_content(row: dict[str, Any]) -> str:
    metadata = row.get('chunk_metadata') or row.get('metadata') or {}
    if not isinstance(metadata, dict):
        metadata = {}

    display_ref = _table_display_ref(row)
    lines = [f"[Table: {display_ref}]" if display_ref else "[Table]"]

    summary = str(metadata.get('summary') or row.get('summary') or '').strip()
    if summary:
        lines.extend(line for line in summary.split('\n') if line.strip())

    keywords = metadata.get('keywords') or row.get('keywords') or []
    if isinstance(keywords, list):
        keyword_text = ';'.join(
            str(keyword).strip() for keyword in keywords if str(keyword).strip()
        )
    else:
        keyword_text = str(keywords or '').strip()
    if keyword_text:
        lines.append(keyword_text)

    caption = str(metadata.get('caption') or row.get('caption') or '').strip()
    if caption:
        lines.append(caption)

    return '\n'.join(lines)


def _table_display_ref(row: dict[str, Any]) -> str:
    for key in ('asset_url', 'file_path', 'source_chunk_path'):
        value = str(row.get(key) or '').strip()
        if value:
            return value

    content = str(row.get('content') or '').strip()
    if content and not content.lstrip().lower().startswith('<table'):
        return content
    return ''
