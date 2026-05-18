from __future__ import annotations

from collections.abc import Iterable


def is_excluded_section(
    *,
    document_id: str | None,
    section_path: str | None,
    exclude_sections: Iterable[dict[str, str]],
) -> bool:
    document_id = str(document_id or '').strip()
    section_path = str(section_path or '').strip()
    if not document_id or not section_path:
        return False
    for item in exclude_sections:
        if not isinstance(item, dict):
            continue
        exc_doc = str(item.get('document_id') or '').strip()
        exc_path = str(item.get('section_path') or '').strip()
        if document_id == exc_doc and (
            section_path == exc_path or section_path.startswith(exc_path + ' / ')
        ):
            return True
    return False
