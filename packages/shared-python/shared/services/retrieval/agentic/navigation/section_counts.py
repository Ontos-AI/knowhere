"""Section count aggregation for agentic navigation."""
from __future__ import annotations

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk


async def attach_section_counts(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    all_sections: dict[str, dict],
    items_by_path: dict[str, dict],
) -> None:
    """Attach direct chunk and connected asset counts to visible section items."""
    scope_item_sids = {
        item["section_id"]
        for item in items_by_path.values()
        if item["show_summary"]
    }
    all_section_ids = [meta["section_id"] for meta in all_sections.values()]
    if not all_section_ids or not scope_item_sids:
        return

    section_id_counts = await _load_direct_chunk_counts(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        all_section_ids=all_section_ids,
    )

    sid_to_path = {meta["section_id"]: path for path, meta in all_sections.items()}
    for section_id, (text_count, image_count, table_count, total_chars) in section_id_counts.items():
        chunk_path = sid_to_path.get(section_id, "")
        if not chunk_path:
            continue

        for item_path, item in items_by_path.items():
            if not item["show_summary"]:
                continue
            if _chunk_belongs_to_item(chunk_path, item_path):
                item["chunk_count"] += text_count
                item["image_count"] += image_count
                item["table_count"] += table_count
                item["total_chars"] += total_chars

    await _attach_connected_asset_counts(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        items_by_path=items_by_path,
        sid_to_path=sid_to_path,
    )

    # Root is a virtual navigation container. Media availability for the
    # whole document is exposed through global SEARCH actions, not as Root
    # node-local images/tables.
    root_item = items_by_path.get("Root")
    if root_item:
        root_item["image_count"] = 0
        root_item["table_count"] = 0


async def _load_direct_chunk_counts(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    all_section_ids: list[str],
) -> dict[str, tuple[int, int, int, int]]:
    chunk_stmt = (
        select(
            DocumentChunk.section_id,
            func.count(
                case(
                    (DocumentChunk.chunk_type.notin_(["image", "table"]), literal_column("1")),
                )
            ).label("text_count"),
            func.count(
                case(
                    (DocumentChunk.chunk_type == "image", literal_column("1")),
                )
            ).label("image_count"),
            func.count(
                case(
                    (DocumentChunk.chunk_type == "table", literal_column("1")),
                )
            ).label("table_count"),
            func.coalesce(
                func.sum(func.length(DocumentChunk.content)), 0
            ).label("total_chars"),
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.section_id.in_(all_section_ids))
        .group_by(DocumentChunk.section_id)
    )
    chunk_rows = (await db.execute(chunk_stmt)).all()
    return {
        section_id: (int(text_count), int(image_count), int(table_count), int(total_chars))
        for section_id, text_count, image_count, table_count, total_chars in chunk_rows
    }


async def _attach_connected_asset_counts(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    items_by_path: dict[str, dict],
    sid_to_path: dict[str, str],
) -> None:
    scope_items_with_zero_assets = [
        item
        for item in items_by_path.values()
        if item["show_summary"] and item["image_count"] == 0 and item["table_count"] == 0
    ]
    if not scope_items_with_zero_assets:
        return

    scope_section_ids = {
        item["section_id"]
        for item in items_by_path.values()
        if item.get("section_id")
    }
    if not scope_section_ids:
        return

    connect_stmt = (
        select(
            DocumentChunk.section_id,
            DocumentChunk.chunk_metadata,
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.section_id.in_(list(scope_section_ids)))
        .where(DocumentChunk.chunk_type == "text")
    )
    connect_result = (await db.execute(connect_stmt)).all()

    section_target_ids: dict[str, set[str]] = {}
    for section_id, metadata in connect_result:
        if not isinstance(metadata, dict):
            continue
        for connection in metadata.get("connect_to") or []:
            target_id = connection.get("target", "")
            if target_id:
                section_target_ids.setdefault(section_id, set()).add(target_id)

    if not section_target_ids:
        return

    all_target_ids: set[str] = set()
    for target_ids in section_target_ids.values():
        all_target_ids.update(target_ids)

    target_type_stmt = (
        select(
            DocumentChunk.chunk_id,
            DocumentChunk.chunk_type,
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.chunk_id.in_(list(all_target_ids)))
        .where(DocumentChunk.chunk_type.in_(["image", "table"]))
    )
    target_type_result = (await db.execute(target_type_stmt)).all()
    target_types = {chunk_id: chunk_type for chunk_id, chunk_type in target_type_result}

    for section_id, target_ids in section_target_ids.items():
        ref_path = sid_to_path.get(section_id, "")
        if not ref_path:
            continue
        referenced_images = sum(1 for target_id in target_ids if target_types.get(target_id) == "image")
        referenced_tables = sum(1 for target_id in target_ids if target_types.get(target_id) == "table")
        if referenced_images == 0 and referenced_tables == 0:
            continue
        for item_path, item in items_by_path.items():
            if not item["show_summary"]:
                continue
            if _chunk_belongs_to_item(ref_path, item_path):
                item["image_count"] += referenced_images
                item["table_count"] += referenced_tables


def _chunk_belongs_to_item(chunk_path: str, item_path: str) -> bool:
    if item_path == "Root":
        return chunk_path == item_path
    return chunk_path == item_path or chunk_path.startswith(item_path + " / ")
