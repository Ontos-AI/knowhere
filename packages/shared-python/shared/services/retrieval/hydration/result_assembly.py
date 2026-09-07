from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.hydration.asset_inline import (
    inline_assets_at_placeholders,
    strip_path_placeholders,
)
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
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
    allowed_chunk_types: set[str] | None = None,
    revision_pins: Mapping[str, str] | None = None,
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
        revision_pins=revision_pins,
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
            assembled_row['content_source'] = 'summary'
            page_nums = extract_page_nums(row)
            if page_nums is not None:
                assembled_row['page_nums'] = page_nums
        elif chunk_type == 'table':
            assembled_row['content'] = _compose_table_content(row, rows_by_chunk_id)
            assembled_row['content_source'] = 'summary'
        elif chunk_type == 'text':
            assembled_row['content'] = _compose_text_content(row, rows_by_chunk_id)
            assembled_row['content_source'] = 'content'
        else:
            assembled_row['content'] = base_content
            assembled_row['content_source'] = 'content'
        assembled_row['content'] = strip_path_placeholders(assembled_row['content'])
        assembled.append(assembled_row)
    return assembled


def _page_summary(row: dict[str, Any]) -> str:
    metadata = row.get('chunk_metadata') or row.get('metadata') or {}
    if not isinstance(metadata, dict):
        return ''
    return str(metadata.get('summary') or '').strip()


def _compose_text_content(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> str:
    base_content = str(row.get('content') or '')
    display_by_target = _connected_display_by_target(row, rows_by_chunk_id)
    if not display_by_target:
        return base_content
    metadata = row.get('chunk_metadata') or row.get('metadata') or {}
    connections = (
        metadata.get('connect_to') if isinstance(metadata, dict) else None
    ) or []
    content, _embedded = inline_assets_at_placeholders(
        base_content,
        connections=connections if isinstance(connections, list) else [],
        display_by_target=display_by_target,
    )
    return content


def _compose_table_content(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> str:
    parts = [_table_summary_content(row)]
    parts.extend(_connected_image_parts(row, rows_by_chunk_id))
    return '\n\n'.join(part for part in parts if part)


def _connected_display_by_target(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> dict[str, str]:
    display: dict[str, str] = {}
    for target_id in iter_connected_target_ids(row):
        target_row = rows_by_chunk_id.get(target_id)
        if not target_row:
            continue
        target_type = normalize_chunk_type(target_row.get('chunk_type'))
        if target_type == 'table':
            target_content = _compose_table_content(target_row, rows_by_chunk_id)
        elif target_type == 'image':
            target_content = _image_display_content(target_row)
        else:
            continue
        if target_content:
            display[target_id] = target_content
    return display


def _connected_image_parts(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> list[str]:
    parts: list[str] = []
    for target_id in iter_connected_target_ids(row):
        target_row = rows_by_chunk_id.get(target_id)
        if not target_row:
            continue
        if normalize_chunk_type(target_row.get('chunk_type')) != 'image':
            continue
        content = _image_display_content(target_row)
        if content:
            parts.append(content)
    return parts


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
