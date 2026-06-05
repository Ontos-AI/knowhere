"""Agentic retrieval navigation tools — observe-act collector model."""
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
    format_items_for_llm,
    format_main_actions_block,
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
    prior_tool_result: dict[str, Any] | None = None,
) -> NavigateStepResult:
    """Navigate one document scope with a single observe-act decision."""
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
            return NavigateStepResult.stop(
                scope_paths[0] if scope_paths else None,
                reason="No visible sections in the current scope.",
            )

        # All items in Section Tree — valid EXPAND targets (includes siblings)
        drillable_items = {item["path"]: item for item in items}
        total_images, total_tables = await count_assets_under_scope(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            scope_paths=scope_paths,
        )

        observation = {
            "visible_sections": [
                item.get("path", "")
                for item in items
                if item.get("path")
            ][:50],
            "available_images": total_images,
            "available_tables": total_tables,
            "prior_tool_result": prior_tool_result,
            "current_scope": scope_path or "root",
        }

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
        expanded_path_set = _expanded_paths_from_trace(nav_trace or [])
        if scope_path:
            expanded_path_set.add(scope_path)
        items_text, overflowed = format_items_for_llm(
            items,
            collected_paths=collected_path_set,
            expanded_paths=expanded_path_set,
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
            main_actions_block=format_main_actions_block(scope_path),
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
        invalid_collect: list[str] = []
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
            elif path:
                invalid_collect.append(path)

        # Validate EXPAND target: must be visible, not collected, not a leaf.
        # Invalid actions are returned as observations for the next loop; the
        # executor does not auto-collect or stop on the model's behalf.
        valid_drill: list[dict[str, Any]] = []
        result_status = "ok"
        result_note: str | None = None
        if action == "EXPAND" and drill_into:
            if drill_into in drillable_items and drill_into not in collected_path_set:
                is_current_scope = (
                    drill_into == scope_path
                    or (scope_path is None and drill_into == "Root")
                )
                if is_current_scope:
                    logger.warning(
                        f"  navigate_step: expand target '{drill_into}' is current scope"
                    )
                    result_status = "invalid_target"
                    result_note = f"expand_target_is_current_scope: {drill_into}"
                    drill_into = None
                elif drillable_items[drill_into].get("is_leaf"):
                    logger.info(
                        f"  navigate_step: expand target '{drill_into}' is a leaf"
                    )
                    result_status = "leaf_target"
                    result_note = f"expand_target_is_leaf_collect_instead: {drill_into}"
                    drill_into = None
                else:
                    valid_drill.append({
                        "path": drill_into,
                        "confidence": 0.8,
                    })
            else:
                logger.warning(
                    f"  navigate_step: expand target '{drill_into}' invalid "
                    f"(not visible or already collected)"
                )
                result_status = "invalid_target"
                result_note = f"expand_target_invalid: {drill_into}"
                drill_into = None

        if invalid_collect and result_status == "ok":
            result_status = "invalid_collect"
            result_note = "invalid_collect_paths: " + ", ".join(invalid_collect[:5])

        back_to = parsed.get("back_to")
        if action == "BACK":
            if scope_path is None:
                result_status = "invalid_back"
                result_note = "already_at_root"
            elif back_to is not None and not scope_path.startswith(back_to + " / "):
                result_status = "invalid_back"
                result_note = f"invalid_back_target: {back_to}"

        # Parse tool parameters for SEARCH
        search_assets_params: dict[str, Any] | None = None

        if action in ("SEARCH_IMAGES", "SEARCH_TABLES"):
            search_query = str(tool_params.get("search_query", query)).strip()
            asset_type = "image" if action == "SEARCH_IMAGES" else "table"
            available_count = total_images if asset_type == "image" else total_tables
            if available_count <= 0:
                result_status = "unavailable_tool"
                result_note = f"{action} unavailable in current scope"
                selected_tools = []
            else:
                search_assets_params = {
                    "query": search_query,
                    "asset_type": asset_type,
                }

        return NavigateStepResult(
            action=action,
            collect=valid_collect,
            drill=valid_drill,
            back_to=back_to,
            tools=selected_tools,
            node=node,
            reason=reason,
            search_assets_params=search_assets_params,
            observation=observation,
            result_status=result_status,
            result_note=result_note,
        )

    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.error(f"  navigate_step failed for doc={document_id}: {exc}")
        return NavigateStepResult.error(
            scope_paths[0] if scope_paths else None,
            reason=str(exc),
        )


def _expanded_paths_from_trace(nav_trace: list[dict[str, Any]]) -> set[str]:
    expanded: set[str] = set()
    for entry in nav_trace:
        if entry.get("action") != "EXPAND":
            continue
        if entry.get("result_status", "ok") != "ok":
            continue
        drill_into = entry.get("drill_into")
        if isinstance(drill_into, str) and drill_into:
            expanded.add(drill_into)
    return expanded
