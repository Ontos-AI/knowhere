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


async def _load_scope_sections(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
) -> list[tuple[str, str]]:
    section_stmt = (
        select(DocumentSection.section_id, DocumentSection.section_path)
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
    )
    if scope_paths:
        scope_filters = []
        for scope in scope_paths:
            scope_filters.append(DocumentSection.section_path == scope)
            scope_filters.append(DocumentSection.section_path.like(f"{scope} / %"))
        section_stmt = section_stmt.where(or_(*scope_filters))
    rows = (await db.execute(section_stmt)).all()
    return [(section_id, section_path or "") for section_id, section_path in rows]


async def count_assets_under_scope(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
) -> tuple[int, int]:
    section_rows = await _load_scope_sections(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_paths=scope_paths,
    )
    all_section_ids = [section_id for section_id, _section_path in section_rows]

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


def build_asset_tools_block(
    total_images: int,
    total_tables: int,
    image_topic_hints: list[str] | None = None,
    table_topic_hints: list[str] | None = None,
) -> str:
    """Build the asset tool description block for the navigation prompt.

    Describes SEARCH_IMAGES, SEARCH_TABLES, and INSPECT_ASSET tools.
    Includes topic hints so the Navigator knows what assets are available
    without seeing the full list.
    """
    if total_images <= 0 and total_tables <= 0:
        return ""

    tools_lines = ["\nAsset tools (usable alongside any action):\n"]
    if total_images > 0:
        hint_text = ""
        if image_topic_hints:
            hint_text = f" Topics include: {', '.join(image_topic_hints[:8])}"
        tools_lines.append(
            f"  SEARCH_IMAGES — Search for images matching a query ({total_images} available).{hint_text}\n"
            f"    Requires: tool_params.search_query (string)\n"
            f"    Returns filtered candidates in next step. Use for targeted image retrieval.\n"
        )
    if total_tables > 0:
        hint_text = ""
        if table_topic_hints:
            hint_text = f" Topics include: {', '.join(table_topic_hints[:8])}"
        tools_lines.append(
            f"  SEARCH_TABLES — Search for tables matching a query ({total_tables} available).{hint_text}\n"
            f"    Requires: tool_params.search_query (string)\n"
            f"    Returns filtered candidates in next step.\n"
        )
    if total_images > 0 or total_tables > 0:
        tools_lines.append(
            f"  INSPECT_ASSET — View detailed description of a specific asset by chunk_id.\n"
            f"    Requires: tool_params.chunk_id (string)\n"
            f"    Use after SEARCH to verify relevance before collecting.\n"
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

        section_rows = await _load_scope_sections(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_list,
        )
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


async def search_assets_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None,
    asset_type: str,
    query: str,
    llm_fn: Any,
) -> list[dict[str, Any]]:
    """LLM-filtered asset search.

    1. Loads all assets of ``asset_type`` under scope via ``asset_filter_step``
    2. Builds a compact candidate list (file_name + description)
    3. Sends to LLM: "which of these match the query?"
    4. Returns only the matching candidates

    This is the only reliable approach without vector/semantic search —
    the LLM understands that "折线图 ≈ 走势图" and "上证指数 ∈ 金融股票".
    """
    t0 = time.monotonic()

    all_assets = await asset_filter_step(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_path=scope_path,
        asset_type=asset_type,
    )
    if not all_assets:
        logger.info(f"  search_assets_step: no {asset_type} assets under scope={scope_path}")
        return []

    # Build compact candidate list for LLM
    candidates_for_llm: list[dict[str, str]] = []
    asset_by_id: dict[str, dict[str, Any]] = {}
    for asset in all_assets:
        chunk_id = str(asset.get("chunk_id") or "")
        if not chunk_id:
            continue
        metadata = asset.get("chunk_metadata") or {}
        summary = metadata.get("summary", "")
        file_path = asset.get("file_path") or ""
        content = asset.get("content") or ""

        # Use summary if available, otherwise first 200 chars of content
        description = summary or content[:200]

        candidates_for_llm.append({
            "id": chunk_id,
            "file": file_path,
            "desc": description[:150],
        })
        asset_by_id[chunk_id] = asset

    if not candidates_for_llm:
        return []

    # LLM filtering
    prompt = _format_asset_filter_prompt(query, asset_type, candidates_for_llm)
    try:
        response = await llm_fn(prompt)
        selected_ids = _parse_asset_filter_response(response, set(asset_by_id.keys()))
    except Exception as exc:
        logger.warning(f"  search_assets_step: LLM filter failed: {exc}, returning empty")
        return []

    # Build result — only matching assets, with relevance info
    result: list[dict[str, Any]] = []
    for chunk_id in selected_ids:
        asset = asset_by_id.get(chunk_id)
        if asset:
            result.append(asset)

    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"  search_assets_step query=\"{query[:50]}\" type={asset_type}: "
        f"{len(result)}/{len(all_assets)} assets matched, {latency}ms"
    )
    return result


def _format_asset_filter_prompt(
    query: str,
    asset_type: str,
    candidates: list[dict[str, str]],
) -> str:
    """Build the LLM prompt for asset relevance filtering."""
    type_label = "images" if asset_type == "image" else "tables"
    items_text = "\n".join(
        f'  {i+1}. id="{c["id"]}" file="{c["file"]}"\n     {c["desc"]}'
        for i, c in enumerate(candidates)
    )
    return (
        f"You are an asset relevance filter.\n\n"
        f"User query: {query}\n\n"
        f"Below are {len(candidates)} {type_label} from a document. "
        f"Select ONLY those that are relevant to the user's query.\n\n"
        f"=== {type_label.title()} ===\n{items_text}\n=== End ===\n\n"
        f"Return ONLY a JSON array of matching asset IDs, e.g.: "
        f'["{candidates[0]["id"]}"]\n'
        f"If none are relevant, return an empty array: []\n"
        f"Do not include any explanation."
    )


def _parse_asset_filter_response(
    text: str,
    valid_ids: set[str],
) -> list[str]:
    """Parse LLM response for asset filter — extract valid chunk IDs."""
    import json
    import re

    text = text.strip()

    # Try direct JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [str(item) for item in result if str(item) in valid_ids]
    except (ValueError, json.JSONDecodeError):
        pass

    # Try extracting from code fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass

    # Try finding any JSON array
    bracket_match = re.search(r"\[.*?\]", text, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group())
            if isinstance(result, list):
                return [str(item) for item in result if str(item) in valid_ids]
        except (ValueError, json.JSONDecodeError):
            pass

    return []


async def inspect_asset_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    chunk_id: str,
) -> dict[str, Any] | None:
    """Load detailed info for a single asset chunk.

    Returns the full description, file_path, section_path, and content
    for the Navigator LLM to make a relevance judgment.
    No extra LLM call — uses the existing parse-time description.
    """
    row = (await db.execute(
        select(
            DocumentChunk.chunk_id,
            DocumentChunk.chunk_type,
            DocumentChunk.content,
            DocumentChunk.file_path,
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
        )
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.chunk_id == chunk_id)
    )).first()

    if not row:
        logger.warning(f"  inspect_asset_step: chunk_id={chunk_id} not found")
        return None

    metadata = row[4] or {}
    return {
        "chunk_id": row[0],
        "chunk_type": row[1],
        "content": (row[2] or "")[:500],
        "file_path": row[3] or "",
        "summary": metadata.get("summary", ""),
        "section_path": row[5] or "",
    }


async def load_asset_topic_hints(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    scope_paths: list[str],
    asset_type: str,
    max_hints: int = 8,
) -> list[str]:
    """Extract brief topic hints from asset file_paths/summaries.

    These are injected into the tool description so the Navigator
    knows what topics are covered without seeing the full list.
    """
    section_rows = await _load_scope_sections(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        scope_paths=scope_paths,
    )
    section_ids = [row[0] for row in section_rows]
    if not section_ids:
        return []

    rows = (await db.execute(
        select(
            DocumentChunk.file_path,
            DocumentChunk.chunk_metadata,
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .where(DocumentChunk.section_id.in_(section_ids))
        .where(DocumentChunk.chunk_type == asset_type)
        .order_by(DocumentChunk.sort_order)
    )).all()

    hints: list[str] = []
    for file_path, metadata in rows:
        # Extract a brief topic from file name or summary
        hint = ""
        if file_path:
            # Strip path prefix and extension: "images/image-9 上证指数走势.jpg" → "上证指数走势"
            name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
            # Remove extension
            name = name.rsplit(".", 1)[0] if "." in name else name
            # Remove common prefixes like "image-9 " or "table-3 "
            import re
            name = re.sub(r"^(?:image|table|img|tbl)-?\d+\s*", "", name, flags=re.IGNORECASE).strip()
            if name:
                hint = name[:30]
        if not hint and isinstance(metadata, dict):
            summary = metadata.get("summary", "")
            if summary:
                # Take first line, skip "image-N" prefix
                first_line = summary.split("\n")[0].strip()
                first_line = re.sub(r"^(?:image|table)-?\d+\s*", "", first_line, flags=re.IGNORECASE).strip()
                hint = first_line[:30]
        if hint and hint not in hints:
            hints.append(hint)
        if len(hints) >= max_hints:
            break

    return hints

