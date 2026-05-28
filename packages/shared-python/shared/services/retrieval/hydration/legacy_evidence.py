from __future__ import annotations

from collections import defaultdict
from typing import Any


def render_legacy_evidence_text(rows: list[dict[str, Any]]) -> str:
    """Render assembled retrieval rows into evidence-only context."""
    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        doc_name = _source_value(row, "source_file_name") or "Unknown document"
        grouped_rows[doc_name].append(row)

    parts: list[str] = []
    for doc_name in sorted(grouped_rows):
        parts.append(f"[Document] {doc_name}")
        last_section = object()
        for row in sorted(grouped_rows[doc_name], key=_row_sort_key):
            section_path = _source_value(row, "section_path") or doc_name
            if section_path != last_section:
                parts.append(f"▸ {section_path}")
                last_section = section_path
            _append_content_lines(parts, row.get("content"))

    return "\n".join(parts)


def _source_value(row: dict[str, Any], key: str) -> str:
    source = row.get("source")
    if isinstance(source, dict):
        value = source.get(key)
        if value:
            return str(value)
    value = row.get(key)
    return str(value) if value else ""


def _row_sort_key(row: dict[str, Any]) -> tuple[str, int, str]:
    section_path = _source_value(row, "section_path")
    try:
        sort_order = int(row.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    chunk_id = str(row.get("chunk_id") or "")
    return section_path, sort_order, chunk_id


def _append_content_lines(parts: list[str], content: object) -> None:
    text = str(content or "").strip()
    if not text:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            parts.append(f"    ┈ {stripped}")
