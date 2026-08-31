from __future__ import annotations

from typing import Any

from shared.services.retrieval.settings import RRF_K


def get_row_path(row: dict[str, Any]) -> str:
    """Extract the canonical path from a row for deduplication."""
    return str(row.get('section_path') or row.get('source_chunk_path') or '')


def merge_channels_rrf(
    channels: list[list[dict[str, Any]]],
    weights: list[float],
    top_k: int,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple retrieval channels."""
    score_dict: dict[str, float] = {}
    row_by_chunk_id: dict[str, dict[str, Any]] = {}

    for channel_idx, channel_rows in enumerate(channels):
        weight = weights[channel_idx] if channel_idx < len(weights) else 1.0
        seen_chunk_ids: set[str] = set()
        unique_rank = 0
        for row in channel_rows:
            chunk_id = str(row.get('chunk_id') or '')
            if not chunk_id or chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            rrf_score = weight / (k + unique_rank + 1)
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + rrf_score
            if chunk_id not in row_by_chunk_id:
                row_by_chunk_id[chunk_id] = row
            unique_rank += 1

    ranked = sorted(score_dict.items(), key=lambda x: x[1], reverse=True)
    results: list[dict[str, Any]] = []
    for chunk_id, fused_score in ranked[:top_k]:
        row = row_by_chunk_id[chunk_id]
        results.append(dict(row, score=round(fused_score, 6)))
    return results


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
