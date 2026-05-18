from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic import asset_tools
from shared.services.retrieval.agentic.types import DocTreeNode
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.path import hydrate_paths_to_rows


async def hydrate_path_selections_into_node(
    db: AsyncSession,
    *,
    node: DocTreeNode,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str,
    job_result_id: str | None = None,
) -> None:
    chunks = await hydrate_paths_to_rows(
        db,
        path_selections=path_selections,
        user_id=user_id,
        namespace=namespace,
        document_id=document_id,
    )
    if not chunks:
        return

    chunks = await _append_connected_asset_targets(db, chunks)
    resolved_job_result_id = job_result_id or _find_job_result_id(chunks)
    if resolved_job_result_id:
        await _attach_root_asset_owners(
            db,
            document_id=document_id,
            job_result_id=resolved_job_result_id,
            chunks=chunks,
        )

    add_chunks_to_node(node, chunks)


async def _append_connected_asset_targets(
    db: AsyncSession, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    connected = await hydrate_connected_target_rows(
        db=db,
        rows=chunks,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    if not connected:
        return chunks

    owner_map = asset_tools.build_connected_owner_map(chunks)
    for chunk in connected:
        if not chunk.get("owner_section_path"):
            chunk["owner_section_path"] = owner_map.get(str(chunk.get("chunk_id") or ""))
    return [*chunks, *connected]


async def _attach_root_asset_owners(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    chunks: list[dict[str, Any]],
) -> None:
    root_map = await asset_tools.resolve_root_asset_owners(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        chunks=chunks,
    )
    if not root_map:
        return

    for chunk in chunks:
        if chunk.get("owner_section_path"):
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        if chunk_id in root_map:
            chunk["owner_section_path"] = root_map[chunk_id]


def _find_job_result_id(chunks: list[dict[str, Any]]) -> str | None:
    return next(
        (str(chunk["job_result_id"]) for chunk in chunks if chunk.get("job_result_id")),
        None,
    )


def add_chunks_to_node(node: DocTreeNode, chunks: list[dict[str, Any]]) -> None:
    for chunk in chunks:
        real_path = (
            chunk.get("owner_section_path")
            or chunk.get("section_path")
            or chunk.get("source_chunk_path")
        )
        if real_path:
            node.add_leaf_chunks(str(real_path), [chunk])
