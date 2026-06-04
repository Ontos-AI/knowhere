"""Agentic retrieval navigation tools — Collector Agent model.

Collector Agent architecture
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Each ``navigate_step`` returns two independent decisions:

- **collect**: paths the agent adds to its evidence collection.
  Collected paths are hydrated with full content after navigation completes.
- **action + drill_into**: navigation direction (DRILL into a section,
  BACK to parent, or STOP).

Asset tools (SEARCH_IMAGES/SEARCH_TABLES) allow the agent
to search media assets. Results are injected into the next
step's prompt context for the agent to act on.
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
    adjust_budget_snapshot,
    format_budget_block,
    parse_collector_response,
)
from shared.services.retrieval.agentic.navigation.section_prompt_projection import (
    format_back_constraint,
    format_back_rule,
    format_drill_constraint,
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
    search_context: str = "",
) -> NavigateStepResult:
    """Navigate one document scope using the Collector Agent model.

    Returns a ``NavigateStepResult`` with:
    - ``collect``: paths to add to the evidence collection
    - ``action``: DRILL/BACK/STOP
    - ``drill``: the single drill target (if action == DRILL)
    - ``tools``: asset tools requested (SEARCH_IMAGES/SEARCH_TABLES)
    - ``search_assets_params``: parsed params for SEARCH_IMAGES/SEARCH_TABLES
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

        # All items in Section Tree — valid DRILL targets (includes siblings)
        drillable_items = {item["path"]: item for item in items}
        # Only current scope children — valid COLLECT targets
        collectable_items = {
            item["path"]: item for item in items if item.get("show_summary", True)
        }
        total_images, total_tables = await count_assets_under_scope(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_paths,
        )

        tools_block = build_asset_tools_block(
            total_images, total_tables,
        )

        # Inject search results from previous step
        if search_context:
            tools_block += f"\n{search_context}\n"

        # Build collected path set for [✓] marking on tree.
        # Exclude outline-mode collections: their children should remain
        # visible and collectable (outline = "see structure, drill deeper").
        collected_path_set = {
            item.get("path", "")
            for item in (collected_paths or [])
            if item.get("hydrate_mode") != "outline"
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

        # Estimate this call's prompt token cost and adjust the budget
        # snapshot so the LLM sees post-call budget, not pre-call.
        # This prevents the LLM from seeing misleadingly low percentages
        # (e.g. 63% when it will actually be 89% after this call).
        prompt_tokens_est = (
            len(items_text) + len(trace_block) + len(tools_block) + 800
        ) // 2  # rough chars-to-tokens ratio
        adjusted_snapshot = adjust_budget_snapshot(
            budget_snapshot, prompt_tokens_est,
        )

        prompt = COLLECTOR_PROMPT.format(
            doc_name=doc_name or document_id,
            doc_id=document_id,
            budget_block=format_budget_block(adjusted_snapshot),
            trace_block=trace_block,
            items_overview=items_text,
            query=query,
            tools_block=tools_block,
            current_scope=scope_path or "root",
            back_rule=format_back_rule(scope_path),
            drill_constraint=format_drill_constraint(scope_path),
            back_constraint=format_back_constraint(scope_path),
        )

        response = await llm_fn(prompt)
        parsed = parse_collector_response(response)
        action = parsed["action"]
        selected_tools = parsed["tools"]
        tool_params = parsed.get("tool_params", {})
        reason = parsed.get("reason", "")
        raw_collect = parsed.get("collect", [])
        drill_into = parsed.get("drill_into")

        scope_label = scope_path or "root"
        logger.info(
            f"  navigate_step scope={scope_label}: "
            f"action={action} collect={len(raw_collect)} "
            f"drill_into={drill_into} tools={selected_tools} "
            f"tool_params={tool_params} "
            f"overflowed={overflowed}"
        )

        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        node.outline_items = [item for item in items if item.get("show_summary", True)]

        # Validate collect paths: must be visible and not already collected
        valid_collect: list[dict[str, Any]] = []
        for item in raw_collect:
            path = item.get("path", "")
            if path in drillable_items and path not in collected_path_set:
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
        fallback_reason: str | None = None
        if action == "DRILL" and drill_into:
            if drill_into in drillable_items and drill_into not in collected_path_set:
                # Guard: prevent drilling into current scope (would loop)
                is_current_scope = (
                    drill_into == scope_path
                    or (scope_path is None and drill_into == "Root")
                )
                if is_current_scope:
                    logger.warning(
                        f"  navigate_step: drill target '{drill_into}' is current scope, "
                        f"auto-collecting visible leaves and stopping"
                    )
                    for vis_path, vis_item in collectable_items.items():
                        if vis_path in collected_path_set:
                            continue
                        if any(c["path"] == vis_path for c in valid_collect):
                            continue
                        if vis_item.get("is_leaf"):
                            node.confidence[vis_path] = 0.5
                            valid_collect.append({
                                "path": vis_path,
                                "confidence": 0.5,
                                "hydrate_mode": "chunks",
                            })
                    action = "STOP"
                    fallback_reason = f"drill_target_is_current_scope: {drill_into}"
                elif drillable_items[drill_into].get("is_leaf"):
                    # Leaf nodes can't be drilled — auto-collect and continue
                    # navigating so the LLM can explore other branches or use
                    # asset tools (SEARCH_IMAGES/SEARCH_TABLES).
                    logger.info(
                        f"  navigate_step: drill target '{drill_into}' is a leaf, "
                        f"auto-collecting and continuing navigation"
                    )
                    if not any(c["path"] == drill_into for c in valid_collect):
                        node.confidence[drill_into] = 0.7
                        valid_collect.append({
                            "path": drill_into,
                            "confidence": 0.7,
                            "hydrate_mode": "chunks",
                        })
                    # Keep action as DRILL but clear drill_into so the loop
                    # stays at the current scope and gives the LLM another
                    # chance to navigate.  Previously this was set to BACK
                    # which caused premature exit when already at root scope.
                    action = "DRILL"
                    drill_into = None
                else:
                    valid_drill.append({
                        "path": drill_into,
                        "confidence": 0.8,
                    })
            else:
                # Invalid drill target — auto-collect all visible leaf children
                # to preserve LLM intent (it clearly found this scope relevant)
                logger.warning(
                    f"  navigate_step: drill target '{drill_into}' invalid "
                    f"(not visible or already collected), "
                    f"auto-collecting visible leaves and stopping"
                )
                for vis_path, vis_item in collectable_items.items():
                    if vis_path in collected_path_set:
                        continue
                    if any(c["path"] == vis_path for c in valid_collect):
                        continue
                    if vis_item.get("is_leaf"):
                        node.confidence[vis_path] = 0.5
                        valid_collect.append({
                            "path": vis_path,
                            "confidence": 0.5,
                            "hydrate_mode": "chunks",
                        })
                action = "STOP"
                fallback_reason = f"drill_target_invalid: {drill_into}"

        # Parse tool parameters for SEARCH
        search_assets_params: dict[str, Any] | None = None

        if "SEARCH_IMAGES" in selected_tools or "SEARCH_TABLES" in selected_tools:
            search_query = str(tool_params.get("search_query", query)).strip()
            if not search_query:
                search_query = query  # Fallback to original query
            asset_type = "image" if "SEARCH_IMAGES" in selected_tools else "table"
            search_assets_params = {
                "query": search_query,
                "asset_type": asset_type,
            }

        # Parse back_to for BACK action
        back_to = parsed.get("back_to")

        return NavigateStepResult(
            action=action,
            collect=valid_collect,
            drill=valid_drill,
            back_to=back_to,
            tools=selected_tools,
            node=node,
            reason=reason,
            fallback_reason=fallback_reason,
            search_assets_params=search_assets_params,
        )

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  navigate_step failed for doc={document_id}: {exc}")
        return NavigateStepResult.error(
            scope_paths[0] if scope_paths else None,
            reason=str(exc),
        )
