"""Persisted outline snapshots for MCP document inspection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

MCP_OUTLINE_SNAPSHOT_METADATA_KEY = "mcp_outline_snapshot"
MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OutlineSectionChunkStats:
    section_id: str | None
    start_chunk: int | None
    end_chunk: int | None
    chunk_count: int
    type_counts: dict[str, int]


def build_mcp_outline_snapshot(
    db: Session,
    *,
    document_id: str,
    job_result_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Build the revision-owned snapshot used by MCP outline inspection."""
    sections = _list_outline_sections(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
    )
    section_stats_by_id, total_chunks, type_counts = _get_outline_chunk_stats(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
    )
    return {
        "schema_version": MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION,
        "job_result_id": job_result_id,
        "job_id": job_id,
        "total_chunks": total_chunks,
        "type_counts": type_counts,
        "sections": _create_section_snapshots(
            sections=sections,
            section_stats_by_id=section_stats_by_id,
        ),
    }


def get_valid_mcp_outline_snapshot(
    document_metadata: Mapping[str, Any] | None,
    *,
    expected_job_result_id: str,
) -> dict[str, Any] | None:
    """Return a version-valid outline snapshot from job result metadata."""
    if not isinstance(document_metadata, Mapping):
        return None
    snapshot = document_metadata.get(MCP_OUTLINE_SNAPSHOT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return None
    schema_version = _read_int(snapshot.get("schema_version"))
    if schema_version != MCP_OUTLINE_SNAPSHOT_SCHEMA_VERSION:
        return None
    snapshot_job_result_id = _read_string(snapshot.get("job_result_id"))
    if snapshot_job_result_id != expected_job_result_id:
        return None
    if _read_int(snapshot.get("total_chunks")) is None:
        return None
    if not isinstance(snapshot.get("type_counts"), Mapping):
        return None
    if not isinstance(snapshot.get("sections"), list):
        return None
    return dict(snapshot)


def _list_outline_sections(
    db: Session,
    *,
    document_id: str,
    job_result_id: str,
) -> list[dict[str, Any]]:
    result = db.execute(
        text(
            """
            SELECT
                section_id,
                section_path,
                section_title,
                section_level,
                summary
            FROM document_sections
            WHERE document_id = :document_id
                AND job_result_id = :job_result_id
            ORDER BY sort_order ASC, created_at ASC, section_id ASC
            """
        ),
        {
            "document_id": document_id,
            "job_result_id": job_result_id,
        },
    )
    return [dict(row) for row in result.mappings().all()]


def _get_outline_chunk_stats(
    db: Session,
    *,
    document_id: str,
    job_result_id: str,
) -> tuple[dict[str | None, OutlineSectionChunkStats], int, dict[str, int]]:
    result = db.execute(
        text(
            """
            SELECT
                dc.section_id,
                lower(dc.chunk_type) AS chunk_type,
                count(*)::integer AS chunk_count,
                min(dc.position)::integer AS start_chunk,
                max(dc.position)::integer AS end_chunk
            FROM document_chunks dc
            WHERE dc.document_id = :document_id
                AND dc.job_result_id = :job_result_id
            GROUP BY dc.section_id, lower(dc.chunk_type)
            ORDER BY min(dc.position) ASC
            """
        ),
        {
            "document_id": document_id,
            "job_result_id": job_result_id,
        },
    )
    section_stats_by_id: dict[str | None, OutlineSectionChunkStats] = {}
    type_counts: dict[str, int] = {}
    total_chunks = 0

    for row in result.mappings().all():
        section_id = cast(str | None, row["section_id"])
        chunk_type = str(row["chunk_type"] or "").strip().lower()
        chunk_count = int(row["chunk_count"])
        start_chunk = int(row["start_chunk"])
        end_chunk = int(row["end_chunk"])
        total_chunks += chunk_count
        type_counts[chunk_type] = type_counts.get(chunk_type, 0) + chunk_count

        current_stats = section_stats_by_id.get(section_id)
        if current_stats is None:
            section_stats_by_id[section_id] = OutlineSectionChunkStats(
                section_id=section_id,
                start_chunk=start_chunk,
                end_chunk=end_chunk,
                chunk_count=chunk_count,
                type_counts={chunk_type: chunk_count},
            )
            continue

        merged_type_counts = dict(current_stats.type_counts)
        merged_type_counts[chunk_type] = (
            merged_type_counts.get(chunk_type, 0) + chunk_count
        )
        section_stats_by_id[section_id] = OutlineSectionChunkStats(
            section_id=section_id,
            start_chunk=(
                min(current_stats.start_chunk, start_chunk)
                if current_stats.start_chunk is not None
                else start_chunk
            ),
            end_chunk=(
                max(current_stats.end_chunk, end_chunk)
                if current_stats.end_chunk is not None
                else end_chunk
            ),
            chunk_count=current_stats.chunk_count + chunk_count,
            type_counts=merged_type_counts,
        )

    return section_stats_by_id, total_chunks, type_counts


def _create_section_snapshots(
    *,
    sections: Sequence[Mapping[str, Any]],
    section_stats_by_id: Mapping[str | None, OutlineSectionChunkStats],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for section in sections:
        section_id = _read_string(section.get("section_id"))
        stats = section_stats_by_id.get(section_id)
        snapshots.append(
            {
                "section_id": section_id or "",
                "section_path": _read_string(section.get("section_path")) or "",
                "section_title": _read_string(section.get("section_title")),
                "section_level": _read_int(section.get("section_level")) or 0,
                "summary": _read_string(section.get("summary")),
                "start_chunk": stats.start_chunk if stats else None,
                "end_chunk": stats.end_chunk if stats else None,
                "chunk_count": stats.chunk_count if stats else 0,
                "type_counts": stats.type_counts if stats else {},
            }
        )
    if snapshots:
        return snapshots

    root_stats = _merge_section_chunk_stats(list(section_stats_by_id.values()))
    return [
        {
            "section_id": "root",
            "section_path": "(root)",
            "section_title": "(root)",
            "section_level": 0,
            "summary": None,
            "start_chunk": root_stats.start_chunk if root_stats else None,
            "end_chunk": root_stats.end_chunk if root_stats else None,
            "chunk_count": root_stats.chunk_count if root_stats else 0,
            "type_counts": root_stats.type_counts if root_stats else {},
        }
    ]


def _merge_section_chunk_stats(
    stats_values: Sequence[OutlineSectionChunkStats],
) -> OutlineSectionChunkStats | None:
    values = list(stats_values)
    if not values:
        return None
    type_counts: dict[str, int] = {}
    start_chunks = [
        stats.start_chunk for stats in values if stats.start_chunk is not None
    ]
    end_chunks = [stats.end_chunk for stats in values if stats.end_chunk is not None]
    for stats in values:
        for chunk_type, count in stats.type_counts.items():
            type_counts[chunk_type] = type_counts.get(chunk_type, 0) + count
    return OutlineSectionChunkStats(
        section_id=None,
        start_chunk=min(start_chunks) if start_chunks else None,
        end_chunk=max(end_chunks) if end_chunks else None,
        chunk_count=sum(stats.chunk_count for stats in values),
        type_counts=type_counts,
    )


def _read_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
