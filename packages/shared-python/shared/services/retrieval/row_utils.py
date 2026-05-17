from __future__ import annotations

import re
from typing import Any

from shared.services.retrieval.section_filters import is_excluded_section

MEDIA_CHUNK_TYPES = {'image', 'table'}
PUBLIC_RESULT_FIELDS = {
    'chunk_type', 'content', 'score', 'asset_url',
}
PUBLIC_SOURCE_FIELDS = {
    'document_id', 'source_file_name', 'section_path',
}

ReferenceLookupKey = tuple[str, str, str, str]

_PATH_REF_RE = re.compile(r'\[(?:images|tables)/[^\]\n]+\]')


def clean_content(content: str) -> str:
    return _PATH_REF_RE.sub('', content).strip()


def normalize_chunk_type(raw: object) -> str:
    return str(raw or '').strip().split('\n', 1)[0].lower()


def is_media_chunk(row: dict[str, Any]) -> bool:
    return normalize_chunk_type(row.get('chunk_type')) in MEDIA_CHUNK_TYPES


def build_reference_lookup_key(
    *,
    document_id: object,
    chunk_id: object,
    section_path: object = '',
    file_path: object = '',
) -> ReferenceLookupKey:
    return (
        str(document_id or '').strip(),
        str(chunk_id or '').strip(),
        str(section_path or '').strip(),
        str(file_path or '').strip(),
    )


def filter_excluded_rows(
    rows: list[dict[str, Any]],
    *,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    excluded_documents = set(exclude_document_ids)
    for row in rows:
        document_id = row.get('document_id')
        if document_id in excluded_documents:
            continue
        if is_excluded_section(
            document_id=document_id,
            section_path=row.get('section_path'),
            exclude_sections=exclude_sections,
        ):
            continue
        filtered.append(row)
    return filtered


def iter_connected_target_ids(row: dict[str, Any]) -> list[str]:
    metadata = row.get('chunk_metadata') or {}
    if not isinstance(metadata, dict):
        return []

    target_ids: list[str] = []
    for item in metadata.get('connect_to') or []:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get('target') or '').strip()
        if target_id:
            target_ids.append(target_id)
    return target_ids
