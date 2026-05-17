from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy import func as sa_func
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult


def build_connected_owner_map(text_chunks: list[dict[str, Any]]) -> dict[str, str]:
    owner_map: dict[str, str] = {}
    for chunk in text_chunks:
        if (chunk.get("chunk_type") or "text") != "text":
            continue
        section_path = chunk.get("section_path") or ""
        if not section_path:
            continue
        metadata = chunk.get("chunk_metadata") or {}
        if not isinstance(metadata, dict):
            continue
        for conn in metadata.get("connect_to") or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get("target") or "").strip()
            if target_id and target_id not in owner_map:
                owner_map[target_id] = section_path
    return owner_map


async def count_assets_under_scope(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
) -> tuple[int, int]:
    scope_section_stmt = (
        select(DocumentSection.section_id)
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
    )
    if scope_paths:
        scope_filters = []
        for scope in scope_paths:
            scope_filters.append(DocumentSection.section_path == scope)
            scope_filters.append(DocumentSection.section_path.like(f"{scope} / %"))
        scope_section_stmt = scope_section_stmt.where(or_(*scope_filters))
    scope_section_ids = await db.execute(scope_section_stmt)
    all_section_ids = [row[0] for row in scope_section_ids.all()]

    if not all_section_ids:
        return 0, 0

    count_stmt = (
        select(
            DocumentChunk.chunk_type,
            sa_func.count(DocumentChunk.id),
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.section_id.in_(all_section_ids))
        .where(DocumentChunk.chunk_type.in_(["image", "table"]))
        .group_by(DocumentChunk.chunk_type)
    )
    count_result = await db.execute(count_stmt)

    total_images = 0
    total_tables = 0
    for chunk_type, count in count_result.all():
        if chunk_type == "image":
            total_images = count
        elif chunk_type == "table":
            total_tables = count
    return total_images, total_tables


def build_asset_tools_block(total_images: int, total_tables: int) -> str:
    if total_images <= 0 and total_tables <= 0:
        return ""

    tools_lines = ["\nOptional asset tools (usable with NAVIGATE or STOP):\n"]
    if total_images > 0:
        tools_lines.append(
            f"  FIND_IMAGES — Extract image/chart assets under the current scope ({total_images} available).\n"
        )
    if total_tables > 0:
        tools_lines.append(
            f"  FIND_TABLES — Extract table/data assets under the current scope ({total_tables} available).\n"
        )
    tools_lines.append(
        "  Note: with NAVIGATE selections, asset tools are limited to the selected sections; "
        "with STOP or no selections, they use the current scope.\n"
    )
    return "".join(tools_lines)


async def resolve_root_asset_owners(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    chunks: list[dict[str, Any]],
) -> dict[str, str]:
    root_asset_ids = [
        str(chunk.get("chunk_id") or "")
        for chunk in chunks
        if not chunk.get("owner_section_path")
        and (chunk.get("section_path") or "") == "Root"
        and (chunk.get("chunk_type") or "").lower() in ("image", "table")
        and chunk.get("chunk_id")
    ]
    if not root_asset_ids:
        return {}

    root_asset_set = set(root_asset_ids)
    text_stmt = (
        select(
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
        )
        .outerjoin(
            DocumentSection,
            DocumentSection.section_id == DocumentChunk.section_id,
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.chunk_type == "text")
    )
    result = await db.execute(text_stmt)

    owner_map: dict[str, str] = {}
    for metadata, section_path in result.all():
        if not isinstance(metadata, dict) or not section_path:
            continue
        for conn in metadata.get("connect_to") or []:
            if not isinstance(conn, dict):
                continue
            target_id = str(conn.get("target") or "").strip()
            if target_id in root_asset_set and target_id not in owner_map:
                owner_map[target_id] = section_path

    if owner_map:
        logger.info(
            f"  resolve_root_asset_owners: resolved {len(owner_map)}/{len(root_asset_ids)} "
            f"Root assets to their owner sections"
        )
    return owner_map


async def asset_filter_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,
) -> list[dict[str, Any]]:
    t0 = time.monotonic()
    try:
        scope_list = (
            scope_path
            if isinstance(scope_path, list)
            else [scope_path]
            if scope_path
            else []
        )

        section_stmt = (
            select(DocumentSection.section_id, DocumentSection.section_path)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
        )
        if scope_list:
            from sqlalchemy import or_

            scope_filters = []
            for scope in scope_list:
                scope_filters.append(DocumentSection.section_path == scope)
                scope_filters.append(DocumentSection.section_path.like(f"{scope} / %"))
            section_stmt = section_stmt.where(or_(*scope_filters))
        section_rows = (await db.execute(section_stmt)).all()
        section_ids = {row[0] for row in section_rows}

        if not section_ids:
            logger.info(f"  asset_filter_step: no sections found under scope={scope_path}")
            return []

        section_path_by_id = {
            section_id: section_path for section_id, section_path in section_rows
        }
        asset_rows = (
            await db.execute(
                select(
                    DocumentChunk.chunk_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.content,
                    DocumentChunk.file_path,
                    DocumentChunk.section_id,
                    DocumentChunk.source_chunk_path,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.sort_order,
                    DocumentChunk.job_result_id,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.section_id.in_(list(section_ids)))
                .where(DocumentChunk.chunk_type == asset_type)
                .order_by(DocumentChunk.sort_order)
            )
        ).all()

        text_rows = (
            await db.execute(
                select(
                    DocumentChunk.section_id,
                    DocumentChunk.chunk_type,
                    DocumentChunk.chunk_metadata,
                    DocumentChunk.source_chunk_path,
                )
                .where(DocumentChunk.document_id == document_id)
                .where(DocumentChunk.job_result_id == job_result_id)
                .where(DocumentChunk.section_id.in_(list(section_ids)))
                .where(DocumentChunk.chunk_type == "text")
            )
        ).all()
        text_row_dicts = [
            {
                "chunk_type": chunk_type,
                "chunk_metadata": metadata or {},
                "section_id": section_id,
                "section_path": section_path_by_id.get(section_id, ""),
                "source_chunk_path": source_chunk_path,
            }
            for section_id, chunk_type, metadata, source_chunk_path in text_rows
        ]
        owner_by_target_id = build_connected_owner_map(text_row_dicts)

        if any(value == "Root" for value in owner_by_target_id.values()):
            doc_stmt = select(Document.source_file_name).where(
                Document.document_id == document_id
            )
            doc_file_name = (await db.execute(doc_stmt)).scalar() or ""
            if doc_file_name:
                for target_id in list(owner_by_target_id):
                    if owner_by_target_id[target_id] == "Root":
                        owner_by_target_id[target_id] = doc_file_name

        connected_target_ids: set[str] = set(owner_by_target_id.keys())
        if connected_target_ids:
            connected_rows = (
                await db.execute(
                    select(
                        DocumentChunk.chunk_id,
                        DocumentChunk.chunk_type,
                        DocumentChunk.content,
                        DocumentChunk.file_path,
                        DocumentChunk.section_id,
                        DocumentChunk.source_chunk_path,
                        DocumentChunk.chunk_metadata,
                        DocumentChunk.sort_order,
                        DocumentChunk.job_result_id,
                    )
                    .where(DocumentChunk.document_id == document_id)
                    .where(DocumentChunk.job_result_id == job_result_id)
                    .where(DocumentChunk.chunk_id.in_(list(connected_target_ids)))
                    .where(DocumentChunk.chunk_type == asset_type)
                    .order_by(DocumentChunk.sort_order)
                )
            ).all()
        else:
            connected_rows = []

        job_id = (
            await db.execute(select(JobResult.job_id).where(JobResult.id == job_result_id))
        ).scalar() or ""
        seen_ids: set[str] = set()
        chunks: list[dict[str, Any]] = []
        for row in list(asset_rows) + list(connected_rows):
            chunk_id = row[0]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            owner_section_path = owner_by_target_id.get(chunk_id)
            if not owner_section_path:
                own_section_path = section_path_by_id.get(row[4])
                if own_section_path and own_section_path == "Root":
                    logger.warning(
                        "  asset_filter_step: rejecting root-level owner fallback "
                        f"chunk_id={chunk_id} section_path={own_section_path}"
                    )
                    own_section_path = None
                owner_section_path = own_section_path

            if not owner_section_path:
                logger.warning(
                    f"  asset_filter_step unresolved owner: chunk_id={chunk_id} "
                    f"file_path={row[3]} scope={scope_path or 'root'}"
                )
                continue

            chunks.append(
                {
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "chunk_type": row[1],
                    "content": row[2],
                    "file_path": row[3],
                    "section_id": row[4],
                    "section_path": owner_section_path,
                    "owner_section_path": owner_section_path,
                    "source_chunk_path": row[5],
                    "chunk_metadata": row[6] or {},
                    "sort_order": row[7],
                    "job_result_id": job_result_id,
                    "job_id": job_id,
                }
            )

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"  asset_filter_step scope={scope_path or 'root'} "
            f"type={asset_type}: {len(chunks)} chunks found, {latency}ms"
        )
        return chunks

    except Exception as exc:
        logger.error(f"  asset_filter_step failed: {exc}")
        return []
