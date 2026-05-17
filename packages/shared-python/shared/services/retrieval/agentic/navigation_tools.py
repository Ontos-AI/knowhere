"""Agentic retrieval navigation tools.

This Module owns document-scope navigation and post-navigation discovery
selection. It keeps the LLM prompt, section traversal, hydration, and asset
owner reconciliation local to the navigation seam.
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy import func as sa_func
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.agentic import asset_tools
from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.prompts import (
    ACTION_PROMPT,
    DISCOVERY_SELECT_PROMPT,
    format_budget_block,
    parse_action_response,
)
from shared.services.retrieval.agentic.section_tree import (
    format_items_for_llm,
    load_child_sections,
)
from shared.services.retrieval.agentic.types import DocTreeNode
from shared.services.retrieval.connected_hydration import hydrate_connected_target_rows
from shared.services.retrieval.path_hydration import hydrate_paths_to_rows
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.retrieval.llm_adapter import LLMFn


_MAX_DISCOVERY_PER_DOC = 3


async def navigate_step(
    db: AsyncSession,
    *,
    document_id: str,
    job_result_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = "",
    scope_path: str | list[str] | None = None,
    exclude_paths: set[str] | None = None,
    revision_hint: str | None = None,
    budget_snapshot: dict | None = None,
) -> tuple[str, list[str], DocTreeNode, list[dict]]:
    """Navigate one document scope and hydrate selected sections."""
    scope_paths = (
        scope_path if isinstance(scope_path, list)
        else [scope_path] if scope_path
        else []
    )
    scope_path_set = set(scope_paths)

    empty = DocTreeNode.empty(scope_paths[0] if scope_paths else None)

    try:
        items = await load_child_sections(
            db,
            document_id,
            job_result_id,
            scope_path,
            exclude_paths=exclude_paths,
        )
        if not items:
            return "STOP", [], empty, []

        selectable = {
            item["path"]: item for item in items if item.get("selectable", False)
        }
        total_images, total_tables = await _count_assets_under_scope(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_paths,
        )
        tools_block = _build_tools_block(total_images, total_tables)

        items_text, overflowed = format_items_for_llm(items)
        prompt = _build_navigation_prompt(
            document_id=document_id,
            doc_name=doc_name,
            query=query,
            scope_paths=scope_paths,
            budget_snapshot=budget_snapshot,
            items_text=items_text,
            tools_block=tools_block,
            revision_hint=revision_hint,
        )

        response = await llm_fn(prompt)
        parsed = parse_action_response(response)
        action = parsed["action"]
        selected_tools = parsed["tools"]
        selections = parsed["selections"]

        scope_label = ", ".join(scope_paths) if scope_paths else "root"
        logger.info(
            f"  navigate_step scope={scope_label}: "
            f"action={action} tools={selected_tools} "
            f"selections={len(selections)} selectable={len(selectable)} "
            f"overflowed={overflowed}"
        )

        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        node.outline_items = [item for item in items if item.get("show_summary", True)]

        valid_selections = [
            selection
            for selection in selections
            if selection["path"] in selectable and selection["path"] not in scope_path_set
        ]

        pending: list[dict] = []
        path_selections: list[dict[str, Any]] = []
        for selection in valid_selections:
            path = selection["path"]
            confidence = selection.get("confidence", 0.7)
            item = selectable[path]
            node.confidence[path] = confidence

            if item.get("is_leaf"):
                path_selections.append({
                    "path": path,
                    "confidence": confidence,
                    "hydrate_mode": "chunks",
                })
            else:
                pending.append({"path": path, "confidence": confidence})
                path_selections.append({
                    "path": path,
                    "confidence": confidence,
                    "hydrate_mode": "self_only",
                })

        await _hydrate_selections_into_node(
            db,
            node=node,
            path_selections=path_selections,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
        )

        return action, selected_tools, node, pending

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  navigate_step failed for doc={document_id}: {exc}")
        return "STOP", [], empty, []


async def discovery_select_step(
    db: AsyncSession,
    *,
    document_id: str,
    query: str,
    llm_fn: LLMFn,
    user_id: str,
    namespace: str,
    doc_name: str = "",
    discovery_hints: list[dict[str, Any]],
    exclude_paths: set[str] | None = None,
    revision_hint: str | None = None,
    budget_snapshot: dict | None = None,
) -> DocTreeNode:
    """Select and hydrate discovery-found sections after BFS navigation."""
    node = DocTreeNode(scope_path=None)
    if not discovery_hints:
        return node

    hints = discovery_hints[:_MAX_DISCOVERY_PER_DOC]

    t0 = time.monotonic()
    try:
        hint_lines, hint_by_path, root_path_selections = _project_discovery_hints(
            hints,
            exclude_paths=exclude_paths,
        )
        if not hint_lines and not root_path_selections:
            return node

        selections: list[dict[str, Any]] = []
        if hint_lines:
            prompt = _build_discovery_selection_prompt(
                document_id=document_id,
                doc_name=doc_name,
                query=query,
                hint_lines=hint_lines,
                revision_hint=revision_hint,
                budget_snapshot=budget_snapshot,
            )
            response = await llm_fn(prompt)
            parsed = parse_action_response(response)
            selections = parsed.get("selections", [])

        logger.info(
            f'  discovery_select_step doc="{doc_name}": '
            f"hints={len(hints)} selections={len(selections)} "
            f"root_selections={len(root_path_selections)}"
        )

        path_selections = _build_discovery_path_selections(
            selections=selections,
            hint_by_path=hint_by_path,
            root_path_selections=root_path_selections,
            node=node,
        )
        await _hydrate_discovery_selections_into_node(
            db,
            node=node,
            path_selections=path_selections,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
        )

        latency = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"  discovery_select_step done: hydrated={len(node.leaf_content)} "
            f"latency={latency}ms"
        )
        return node

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  discovery_select_step failed for doc={document_id}: {exc}")
        return node


async def _count_assets_under_scope(
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


def _build_tools_block(total_images: int, total_tables: int) -> str:
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


def _build_navigation_prompt(
    *,
    document_id: str,
    doc_name: str,
    query: str,
    scope_paths: list[str],
    budget_snapshot: dict | None,
    items_text: str,
    tools_block: str,
    revision_hint: str | None,
) -> str:
    if not scope_paths:
        scope_header = "Current scope: root (document top level)"
    elif len(scope_paths) == 1:
        scope_header = f'Current scope: navigating into "{scope_paths[0]}"'
    else:
        scope_header = f"Current scope: navigating into {len(scope_paths)} sections"

    prompt = ACTION_PROMPT.format(
        doc_name=doc_name or document_id,
        doc_id=document_id,
        scope_header=scope_header,
        budget_block=format_budget_block(budget_snapshot),
        items_overview=items_text,
        query=query,
        tools_block=tools_block,
    )
    if revision_hint:
        prompt += (
            "\n\nIMPORTANT: Previous round feedback: "
            f'"{revision_hint}". Adjust your selections accordingly.'
        )
    return prompt


async def _hydrate_selections_into_node(
    db: AsyncSession,
    *,
    node: DocTreeNode,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str,
    job_result_id: str,
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

    connected = await hydrate_connected_target_rows(
        db=db,
        rows=chunks,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    if connected:
        owner_map = asset_tools.build_connected_owner_map(chunks)
        for chunk in connected:
            if not chunk.get("owner_section_path"):
                chunk["owner_section_path"] = owner_map.get(str(chunk.get("chunk_id") or ""))
        chunks = chunks + connected

    root_map = await asset_tools.resolve_root_asset_owners(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        chunks=chunks,
    )
    if root_map:
        for chunk in chunks:
            if chunk.get("owner_section_path"):
                continue
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id in root_map:
                chunk["owner_section_path"] = root_map[chunk_id]

    _add_chunks_to_node(node, chunks)


def _project_discovery_hints(
    hints: list[dict[str, Any]],
    *,
    exclude_paths: set[str] | None,
) -> tuple[list[str], dict[str, dict], list[dict[str, Any]]]:
    exclude_set = {
        normalize_section_path(path)
        for path in (exclude_paths or set())
        if path
    }
    hint_lines: list[str] = []
    hint_by_path: dict[str, dict] = {}
    root_path_selections: list[dict[str, Any]] = []
    for hint in hints:
        section_path = normalize_section_path(hint.get("section_path", ""))
        if not section_path:
            continue
        if section_path in exclude_set:
            continue
        if section_path in hint_by_path:
            continue

        hint_by_path[section_path] = hint
        if section_path == "Root":
            root_path_selections.append({
                "path": section_path,
                "confidence": float(
                    hint.get("discovery_score") or hint.get("score") or 0.7
                ),
                "hydrate_mode": "self_only",
            })
            continue

        summary = hint.get("summary", "") or ""
        hint_lines.append(f'▸ path="{section_path}"')
        if summary:
            hint_lines.append(f"    {summary[:300]}")

    return hint_lines, hint_by_path, root_path_selections


def _build_discovery_selection_prompt(
    *,
    document_id: str,
    doc_name: str,
    query: str,
    hint_lines: list[str],
    revision_hint: str | None,
    budget_snapshot: dict | None,
) -> str:
    revision_context = ""
    if revision_hint:
        revision_context = (
            "\nIMPORTANT: This is a REVISION round. "
            "The previous search attempt failed because:\n"
            f'"{revision_hint}"\n'
            "Adjust your selection accordingly. "
            "If no candidate is relevant, return an EMPTY list [].\n"
        )

    return DISCOVERY_SELECT_PROMPT.format(
        doc_name=doc_name or document_id,
        budget_block=format_budget_block(budget_snapshot),
        items="\n".join(hint_lines),
        query=query,
        revision_context=revision_context,
    )


def _build_discovery_path_selections(
    *,
    selections: list[dict[str, Any]],
    hint_by_path: dict[str, dict],
    root_path_selections: list[dict[str, Any]],
    node: DocTreeNode,
) -> list[dict[str, Any]]:
    valid_selections = [
        selection for selection in selections if selection["path"] in hint_by_path
    ]
    path_selections = list(root_path_selections)
    for selection in valid_selections:
        path = selection["path"]
        confidence = selection.get("confidence", 0.7)
        node.confidence[path] = confidence
        path_selections.append({"path": path, "confidence": confidence})

    if not path_selections and hint_by_path:
        fallback_path, fallback_hint = next(iter(hint_by_path.items()))
        fallback_confidence = float(
            fallback_hint.get("discovery_score")
            or fallback_hint.get("score")
            or 0.5
        )
        node.confidence[fallback_path] = fallback_confidence
        path_selections.append({
            "path": fallback_path,
            "confidence": fallback_confidence,
            "hydrate_mode": "self_only",
        })

    return path_selections


async def _hydrate_discovery_selections_into_node(
    db: AsyncSession,
    *,
    node: DocTreeNode,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str,
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

    connected = await hydrate_connected_target_rows(
        db=db,
        rows=chunks,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    if connected:
        owner_map = asset_tools.build_connected_owner_map(chunks)
        for chunk in connected:
            if not chunk.get("owner_section_path"):
                chunk["owner_section_path"] = owner_map.get(str(chunk.get("chunk_id") or ""))
        chunks = chunks + connected

    job_result_id = next(
        (str(chunk["job_result_id"]) for chunk in chunks if chunk.get("job_result_id")),
        None,
    )
    root_map = (
        await asset_tools.resolve_root_asset_owners(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            chunks=chunks,
        )
        if job_result_id
        else {}
    )
    if root_map:
        for chunk in chunks:
            if chunk.get("owner_section_path"):
                continue
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id in root_map:
                chunk["owner_section_path"] = root_map[chunk_id]

    _add_chunks_to_node(node, chunks)


def _add_chunks_to_node(node: DocTreeNode, chunks: list[dict[str, Any]]) -> None:
    for chunk in chunks:
        real_path = (
            chunk.get("owner_section_path")
            or chunk.get("section_path")
            or chunk.get("source_chunk_path")
        )
        if real_path:
            node.add_leaf_chunks(str(real_path), [chunk])
