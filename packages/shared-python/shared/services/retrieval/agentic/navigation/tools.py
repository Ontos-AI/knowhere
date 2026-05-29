"""Agentic retrieval navigation tools.

This Module owns document-scope navigation and post-navigation discovery
selection. It keeps the LLM prompt, section traversal, hydration, and asset
owner reconciliation local to the navigation seam.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.navigation.assets import (
    build_asset_tools_block,
    count_assets_under_scope,
)
from shared.services.retrieval.agentic.core.budget import BudgetExceeded
from shared.services.retrieval.agentic.prompts import (
    ACTION_PROMPT,
    format_budget_block,
    parse_action_response,
)
from shared.services.retrieval.agentic.navigation.section_prompt_projection import format_items_for_llm
from shared.services.retrieval.agentic.navigation.section_tree import load_child_sections
from shared.services.retrieval.agentic.navigation.selection_hydration import (
    hydrate_path_selections_into_node,
)
from shared.services.retrieval.agentic.core.types import DocTreeNode, NavigateStepResult
from shared.services.retrieval.llm_adapter import LLMFn


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
    budget_snapshot: dict | None = None,
) -> NavigateStepResult:
    """Navigate one document scope and hydrate selected sections."""
    scope_paths = (
        scope_path if isinstance(scope_path, list)
        else [scope_path] if scope_path
        else []
    )
    scope_path_set = set(scope_paths)


    try:
        items = await load_child_sections(
            db,
            document_id,
            job_result_id,
            scope_path,
            exclude_paths=exclude_paths,
        )
        if not items:
            return NavigateStepResult.stop(scope_paths[0] if scope_paths else None)

        selectable = {
            item["path"]: item for item in items if item.get("selectable", False)
        }
        visible_items = {
            item["path"]: item for item in items if item.get("show_summary", True)
        }
        total_images, total_tables = await count_assets_under_scope(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_paths,
        )
        tools_block = build_asset_tools_block(total_images, total_tables)

        items_text, overflowed = format_items_for_llm(items)
        prompt = _build_navigation_prompt(
            document_id=document_id,
            doc_name=doc_name,
            query=query,
            scope_paths=scope_paths,
            budget_snapshot=budget_snapshot,
            items_text=items_text,
            tools_block=tools_block,
        )

        response = await llm_fn(prompt)
        parsed = parse_action_response(response)
        action = parsed["action"]
        selected_tools = parsed["tools"]
        selections = parsed["selections"]
        reason = parsed.get("reason", "")
        stop_type = parsed.get("stop_type", "")

        scope_label = ", ".join(scope_paths) if scope_paths else "root"
        logger.info(
            f"  navigate_step scope={scope_label}: "
            f"action={action} tools={selected_tools} "
            f"selections={len(selections)} selectable={len(selectable)} "
            f"overflowed={overflowed}"
        )

        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        node.outline_items = [item for item in items if item.get("show_summary", True)]

        raw_valid_selections = [
            selection
            for selection in selections
            if selection["path"] in visible_items and selection["path"] not in scope_path_set
        ]
        valid_selections = _dedupe_selected_ancestors(raw_valid_selections)

        pending: list[dict] = []
        path_selections: list[dict[str, Any]] = []
        for selection in valid_selections:
            path = selection["path"]
            confidence = selection.get("confidence", 0.7)
            item = visible_items[path]
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

        await hydrate_path_selections_into_node(
            db,
            node=node,
            path_selections=path_selections,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
            job_result_id=job_result_id,
        )

        return NavigateStepResult(
            action=action,
            tools=selected_tools,
            node=node,
            pending=pending,
            reason=reason,
            stop_type=stop_type,
        )

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  navigate_step failed for doc={document_id}: {exc}")
        return NavigateStepResult.stop(scope_paths[0] if scope_paths else None)
def _build_navigation_prompt(
    *,
    document_id: str,
    doc_name: str,
    query: str,
    scope_paths: list[str],
    budget_snapshot: dict | None,
    items_text: str,
    tools_block: str,
) -> str:
    if not scope_paths:
        scope_header = "Current scope: root (document top level)"
    elif len(scope_paths) == 1:
        scope_header = f'Current scope: navigating into "{scope_paths[0]}"'
    else:
        scope_header = f"Current scope: navigating into {len(scope_paths)} sections"

    return ACTION_PROMPT.format(
        doc_name=doc_name or document_id,
        doc_id=document_id,
        scope_header=scope_header,
        budget_block=format_budget_block(budget_snapshot),
        items_overview=items_text,
        query=query,
        tools_block=tools_block,
    )


def _dedupe_selected_ancestors(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep coarser selected ancestors when both parent and child paths are selected."""
    selected_paths = [str(selection.get("path") or "") for selection in selections]
    kept: list[dict[str, Any]] = []
    for selection in selections:
        path = str(selection.get("path") or "")
        if not path:
            continue
        if any(
            other != path and path.startswith(other + " / ")
            for other in selected_paths
        ):
            continue
        kept.append(selection)
    return kept
