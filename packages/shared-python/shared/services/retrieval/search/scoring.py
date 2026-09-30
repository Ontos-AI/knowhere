from __future__ import annotations

from typing import Any


def get_row_path(row: dict[str, Any]) -> str:
    """Extract the canonical path from a row for deduplication."""
    return str(row.get('section_path') or row.get('source_chunk_path') or '')


def normalize_row_scores(
    rows: list[dict[str, Any]],
    *,
    source_field: str,
    target_field: str,
    default: float,
) -> None:
    if not rows:
        return
    values = [float(row.get(source_field, 0.0) or 0.0) for row in rows]
    min_score = min(values)
    max_score = max(values)
    if max_score <= 0.0 and min_score <= 0.0:
        for row in rows:
            row[target_field] = 0.0
        return
    if max_score == min_score:
        for row in rows:
            row[target_field] = default
        return
    denominator = max_score - min_score
    for row in rows:
        raw_score = float(row.get(source_field, 0.0) or 0.0)
        row[target_field] = round((raw_score - min_score) / denominator, 6)
