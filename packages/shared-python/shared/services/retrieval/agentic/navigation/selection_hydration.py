from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.chunks.same_as_markers import MEDIA_RELATIONS, SAME_AS_RELATION
from shared.services.retrieval.agentic.core.types import DocTreeNode
from shared.services.retrieval.agentic.navigation import assets as asset_tools
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.path import hydrate_paths_to_rows
from shared.services.retrieval.hydration.reference import hydrate_referenced_chunk_rows
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type
from shared.services.retrieval.hydration.same_as import (
    iter_connections,
    materialize_page_evidence,
)


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

    chunks = await _materialize_same_as_and_append_media(db, chunks)
    resolved_job_result_id = job_result_id or _find_job_result_id(chunks)
    if resolved_job_result_id:
        await _attach_root_asset_owners(
            db,
            document_id=document_id,
            job_result_id=resolved_job_result_id,
            chunks=chunks,
        )

    add_chunks_to_node(node, chunks)


async def hydrate_chunk_refs_into_node(
    db: AsyncSession,
    *,
    node: DocTreeNode,
    refs: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str,
    job_result_id: str | None = None,
) -> None:
    chunks = await hydrate_referenced_chunk_rows(
        db=db,
        user_id=user_id,
        namespace=namespace,
        refs=refs,
    )
    if not chunks:
        return

    chunks = await _materialize_same_as_and_append_media(db, chunks)
    resolved_job_result_id = job_result_id or _find_job_result_id(chunks)
    if resolved_job_result_id:
        await _attach_root_asset_owners(
            db,
            document_id=document_id,
            job_result_id=resolved_job_result_id,
            chunks=chunks,
        )

    add_chunks_to_node(node, chunks)


async def _materialize_same_as_and_append_media(
    db: AsyncSession,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve SAME-AS onto selected page rows, then attach media only."""
    same_as_owners = await hydrate_connected_target_rows(
        db=db,
        rows=chunks,
        exclude_document_ids=[],
        exclude_sections=[],
        relations={SAME_AS_RELATION},
        target_chunk_types={"page"},
    )
    rows_by_chunk_id = {
        str(row.get("chunk_id") or ""): row
        for row in [*chunks, *same_as_owners]
        if row.get("chunk_id")
    }

    materialized: list[dict[str, Any]] = []
    for chunk in chunks:
        if normalize_chunk_type(chunk.get("chunk_type")) == "page":
            updated = materialize_page_evidence(
                chunk,
                rows_by_chunk_id=rows_by_chunk_id,
            )
            updated.pop("_content_chunk_ids", None)
            materialized.append(updated)
        else:
            materialized.append(chunk)

    return await _append_connected_asset_targets(db, materialized)


async def _append_connected_asset_targets(
    db: AsyncSession, chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    connected = await hydrate_connected_target_rows(
        db=db,
        rows=chunks,
        exclude_document_ids=[],
        exclude_sections=[],
        relations=MEDIA_RELATIONS,
        target_chunk_types={"image", "table"},
    )
    if not connected:
        return chunks

    connected_by_id = {
        str(chunk.get("chunk_id") or ""): chunk
        for chunk in connected
        if chunk.get("chunk_id")
    }
    expanded = list(chunks)
    seen_mounts: set[tuple[str, str]] = set()
    for selected in chunks:
        selected_type = normalize_chunk_type(selected.get("chunk_type"))
        if selected_type not in {"text", "page"}:
            continue
        section_path = str(selected.get("section_path") or "").strip()
        if not section_path:
            continue
        for connection in iter_connections(selected, relations=MEDIA_RELATIONS):
            target_id = str(connection.get("target") or "").strip()
            asset = connected_by_id.get(target_id)
            if asset is None:
                continue
            mount_key = (target_id, section_path)
            if mount_key in seen_mounts:
                continue
            seen_mounts.add(mount_key)
            mounted = dict(asset)
            mounted["owner_section_path"] = section_path
            expanded.append(mounted)
    return expanded


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
