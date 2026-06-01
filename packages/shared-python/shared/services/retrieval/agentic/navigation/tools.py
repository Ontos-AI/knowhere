"""Agentic retrieval navigation tools — Collector Agent model.

Collector Agent architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Each ``navigate_step`` returns two independent decisions:

- **collect**: paths the agent adds to its evidence collection.
  Collected paths are hydrated with full content after navigation completes.
- **action + drill_into**: navigation direction (DRILL into a section,
  BACK to parent, or STOP).

Asset collection (images/tables) still runs during navigation so LLM
tool requests are honoured, but assets are reconciled after hydration.
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
    COLLECTOR_PROMPT,
    format_budget_block,
    parse_collector_response,
)
from shared.services.retrieval.agentic.navigation.section_prompt_projection import (
    format_items_for_llm,
    format_nav_trace,
)
from shared.services.retrieval.agentic.navigation.section_tree import load_child_sections
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
    scope_path: str | None = None,
    exclude_paths: set[str] | None = None,
    budget_snapshot: dict | None = None,
    nav_trace: list[dict[str, Any]] | None = None,
    collected_paths: list[dict[str, Any]] | None = None,
) -> NavigateStepResult:
    """Navigate one document scope using the Collector Agent model.

    Returns a ``NavigateStepResult`` with:
    - ``collect``: paths to add to the evidence collection
    - ``action``: DRILL/BACK/STOP
    - ``drill``: the single drill target (if action == DRILL)
    - ``tools``: asset tool invocations
    - ``node``: outline tree node for rendering context
    """
    scope_paths = [scope_path] if scope_path else []

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

        # Build collected path set for [✓] marking on tree
        collected_path_set = {
            item.get("path", "") for item in (collected_paths or [])
        }
        items_text, overflowed = format_items_for_llm(
            items,
            collected_paths=collected_path_set,
        )

        # Build trace block (unified: scope + actions + collection)
        trace_block = format_nav_trace(
            nav_trace or [],
            collected_paths or [],
        )

        prompt = COLLECTOR_PROMPT.format(
            doc_name=doc_name or document_id,
            doc_id=document_id,
            budget_block=format_budget_block(budget_snapshot),
            trace_block=trace_block,
            items_overview=items_text,
            query=query,
            tools_block=tools_block,
        )

        response = await llm_fn(prompt)
        parsed = parse_collector_response(response)
        action = parsed["action"]
        selected_tools = parsed["tools"]
        reason = parsed.get("reason", "")
        raw_collect = parsed.get("collect", [])
        drill_into = parsed.get("drill_into")

        scope_label = scope_path or "root"
        logger.info(
            f"  navigate_step scope={scope_label}: "
            f"action={action} collect={len(raw_collect)} "
            f"drill_into={drill_into} tools={selected_tools} "
            f"overflowed={overflowed}"
        )

        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        node.outline_items = [item for item in items if item.get("show_summary", True)]

        # Validate collect paths: must be visible and not already collected
        valid_collect: list[dict[str, Any]] = []
        for item in raw_collect:
            path = item.get("path", "")
            if path in visible_items and path not in collected_path_set:
                confidence = item.get("confidence", 0.7)
                outline = item.get("outline", False)
                node.confidence[path] = confidence
                valid_collect.append({
                    "path": path,
                    "confidence": confidence,
                    "hydrate_mode": "outline" if outline else "chunks",
                })

        # Validate drill target: must be visible, not collected, not a leaf
        valid_drill: list[dict[str, Any]] = []
        if action == "DRILL" and drill_into:
            if drill_into in visible_items and drill_into not in collected_path_set:
                drill_item = visible_items[drill_into]
                if drill_item.get("is_leaf"):
                    # Leaf nodes can't be drilled — auto-collect instead
                    logger.info(
                        f"  navigate_step: drill target '{drill_into}' is a leaf, "
                        f"auto-collecting instead"
                    )
                    if not any(c["path"] == drill_into for c in valid_collect):
                        node.confidence[drill_into] = 0.7
                        valid_collect.append({
                            "path": drill_into,
                            "confidence": 0.7,
                            "hydrate_mode": "chunks",
                        })
                    action = "STOP"  # no valid drill target
                else:
                    valid_drill.append({
                        "path": drill_into,
                        "confidence": 0.8,
                    })
            else:
                logger.warning(
                    f"  navigate_step: drill target '{drill_into}' invalid "
                    f"(not visible or already collected), falling back to STOP"
                )
                action = "STOP"

        return NavigateStepResult(
            action=action,
            collect=valid_collect,
            drill=valid_drill,
            tools=selected_tools,
            node=node,
            reason=reason,
        )

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  navigate_step failed for doc={document_id}: {exc}")
        return NavigateStepResult.stop(scope_paths[0] if scope_paths else None)

