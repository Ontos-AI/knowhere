"""Agentic retrieval navigation tools — observe-act collector model."""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.navigation.assets import (
    count_assets_under_scope,
)
from shared.services.retrieval.agentic.core.budget import (
    BudgetExceeded,
    budget_status_from_snapshot,
)
from shared.services.retrieval.agentic.prompts import (
    COLLECTOR_PROMPT,
    adjust_budget_snapshot,
    parse_collector_response,
)
from shared.services.retrieval.agentic.navigation.section_prompt_projection import (
    format_nav_trace,
)
from shared.services.retrieval.agentic.navigation.actions import (
    build_legal_actions,
    format_actionable_observation,
    format_agent_state_block,
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
    expanded_scopes: set[str] | None = None,
    rejected_paths: set[str] | None = None,
    rejected_collect_paths: set[str] | None = None,
    disabled_asset_types: set[str] | None = None,
    discovery_hints: list[dict[str, Any]] | None = None,
    section_rows: list | None = None,
    query_intent: str = "UNKNOWN",
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
            section_rows=section_rows,
        )
        if not items:
            return NavigateStepResult.stop(
                scope_paths[0] if scope_paths else None,
                reason="No visible sections in the current scope.",
            )

        budget_status = budget_status_from_snapshot(budget_snapshot)
        if budget_status in {"CRITICAL", "EXHAUSTED"}:
            total_images, total_tables = 0, 0
        else:
            total_images, total_tables = await count_assets_under_scope(
                db,
                document_id=document_id,
                job_result_id=job_result_id,
                scope_paths=scope_paths,
            )

        expanded_path_set = set(expanded_scopes or _expanded_paths_from_trace(nav_trace or []))
        if scope_path:
            expanded_path_set.add(scope_path)
        provisional_action_set = build_legal_actions(
            items=items,
            current_scope=scope_path,
            collected_paths=collected_paths or [],
            expanded_scopes=expanded_path_set,
            discovery_hints=discovery_hints,
            rejected_paths=rejected_paths or set(),
            rejected_collect_paths=rejected_collect_paths or set(),
            total_images=total_images,
            total_tables=total_tables,
            disabled_asset_types=disabled_asset_types or set(),
            budget_snapshot=budget_snapshot,
        )
        provisional_observation_text, provisional_overflowed = (
            format_actionable_observation(
                items=items,
                action_set=provisional_action_set,
            )
        )

        trace_block = format_nav_trace(nav_trace or [])

        # Estimate this call's prompt token cost and adjust the budget
        # snapshot so the LLM sees post-call budget, not pre-call.
        # This prevents the LLM from seeing misleadingly low percentages
        # (e.g. 63% when it will actually be 89% after this call).
        prompt_tokens_est = (
            len(provisional_observation_text)
            + len(trace_block)
            + 800
        ) // 2  # rough chars-to-tokens ratio
        adjusted_snapshot = adjust_budget_snapshot(
            budget_snapshot, prompt_tokens_est,
        )
        if (
            budget_status_from_snapshot(adjusted_snapshot)
            == budget_status_from_snapshot(budget_snapshot)
        ):
            action_set = provisional_action_set
            actionable_observation = provisional_observation_text
            overflowed = provisional_overflowed
        else:
            action_set = build_legal_actions(
                items=items,
                current_scope=scope_path,
                collected_paths=collected_paths or [],
                expanded_scopes=expanded_path_set,
                discovery_hints=discovery_hints,
                rejected_paths=rejected_paths or set(),
                rejected_collect_paths=rejected_collect_paths or set(),
                total_images=total_images,
                total_tables=total_tables,
                disabled_asset_types=disabled_asset_types or set(),
                budget_snapshot=adjusted_snapshot,
            )
            actionable_observation, overflowed = format_actionable_observation(
                items=items,
                action_set=action_set,
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
            "query_intent": query_intent,
            "legal_actions": {
                "expand": [item.id for item in action_set.expand],
                "collect": [item.id for item in action_set.collect],
                "back": [item.id for item in action_set.back],
                "search": [item.id for item in action_set.search],
                "finish": [action_set.finish.id] if action_set.finish else [],
            },
            "rejected_paths": sorted(rejected_paths or set()),
            "rejected_collect_paths": sorted(rejected_collect_paths or set()),
        }
        agent_state_block = format_agent_state_block(
            current_scope=scope_path,
            query_intent=query_intent,
            expanded_scopes=expanded_path_set,
            rejected_paths=rejected_paths or set(),
            collected_paths=collected_paths or [],
            rejected_collect_paths=rejected_collect_paths or set(),
            prior_tool_result=prior_tool_result,
            search_context=search_context,
            budget_snapshot=adjusted_snapshot,
        )

        prompt = COLLECTOR_PROMPT.format(
            doc_name=doc_name or document_id,
            doc_id=document_id,
            agent_state_block=agent_state_block,
            trace_block=trace_block,
            query=query,
            actionable_observation=actionable_observation,
        )

        response = await llm_fn(prompt)
        parsed = parse_collector_response(response)
        requested_action = parsed["action"]
        selected_tools = parsed["tools"]
        tool_params = parsed.get("tool_params", {})
        reason = parsed.get("reason", "")
        raw_collect = parsed.get("collect", [])
        action_id = parsed.get("action_id")
        legal_main = action_set.get(action_id)
        action = (
            legal_main.action
            if legal_main and legal_main.action != "COLLECT"
            else requested_action
        )
        if action != requested_action:
            reason = (
                f"Action field '{requested_action}' did not match legal action "
                f"ID '{action_id}'; executing ID-defined action '{action}'. "
                + reason
            ).strip()[:500]
            selected_tools = [action] if action in ("SEARCH_IMAGES", "SEARCH_TABLES") else []

        scope_label = scope_path or "root"
        logger.info(
            f"  navigate_step scope={scope_label}: "
            f"action={action} collect={len(raw_collect)} "
            f"action_id={action_id} tools={selected_tools} "
            f"tool_params={tool_params} "
            f"overflowed={overflowed}"
        )

        node = DocTreeNode(scope_path=scope_paths[0] if scope_paths else None)
        node.outline_items = [item for item in items if item.get("show_summary", True)]

        # Resolve COLLECT side effects from legal action IDs.
        valid_collect: list[dict[str, Any]] = []
        invalid_collect: list[str] = []
        existing_collect_modes = _existing_collect_modes(collected_paths or [])
        for item in raw_collect:
            collect_id = item.get("id")
            legal_collect = action_set.get(collect_id)
            if legal_collect and legal_collect.action == "COLLECT" and legal_collect.path:
                confidence = item.get("confidence", 0.7)
                outline = bool(item.get("outline", False)) and (
                    budget_status_from_snapshot(adjusted_snapshot) != "CRITICAL"
                    or query_intent in {"MACRO_SUMMARY", "STRUCTURE_OVERVIEW"}
                )
                if (
                    legal_main
                    and legal_main.action == "EXPAND"
                    and legal_main.path == legal_collect.path
                ):
                    outline = True
                hydrate_mode = "outline" if outline else "chunks"
                existing_mode = existing_collect_modes.get(legal_collect.path)
                if existing_mode == "chunks":
                    continue
                if existing_mode == "outline" and hydrate_mode == "outline":
                    continue
                node.confidence[legal_collect.path] = confidence
                valid_collect.append({
                    "path": legal_collect.path,
                    "confidence": confidence,
                    "hydrate_mode": hydrate_mode,
                })
            elif collect_id:
                invalid_collect.append(str(collect_id))

        valid_drill: list[dict[str, Any]] = []
        result_status = "ok"
        result_note: str | None = None
        drill_into: str | None = None
        back_to: str | None = None
        if requested_action == "ERROR":
            result_status = "invalid_response"
            result_note = reason or "invalid model response"
        elif action == "EXPAND":
            if legal_main and legal_main.action == "EXPAND" and legal_main.path:
                drill_into = legal_main.path
                valid_drill.append({
                    "path": drill_into,
                    "confidence": 0.8,
                })
            else:
                result_status = "invalid_action_id"
                result_note = f"invalid_expand_id: {action_id}"
        elif action == "BACK":
            if legal_main and legal_main.action == "BACK":
                back_to = legal_main.target_scope
            else:
                result_status = "invalid_action_id"
                result_note = f"invalid_back_id: {action_id}"
        elif action in ("SEARCH_IMAGES", "SEARCH_TABLES"):
            if legal_main is None or legal_main.action != action:
                result_status = "invalid_action_id"
                result_note = f"invalid_search_id: {action_id}"
        elif action == "FINISH":
            if legal_main is None or legal_main.action != "FINISH":
                result_status = "invalid_action_id"
                result_note = f"invalid_finish_id: {action_id}"

        if invalid_collect and result_status == "ok":
            result_status = "invalid_collect"
            result_note = "invalid_collect_ids: " + ", ".join(invalid_collect[:5])

        # Parse tool parameters for SEARCH
        search_assets_params: dict[str, Any] | None = None

        if action in ("SEARCH_IMAGES", "SEARCH_TABLES") and result_status == "ok":
            asset_type = "image" if action == "SEARCH_IMAGES" else "table"
            collected_scope_paths = [
                str(item.get("path") or "")
                for item in valid_collect
                if item.get("path")
            ]
            search_assets_params = {
                "query": query.strip(),
                "asset_type": asset_type,
                "scope_paths": collected_scope_paths or scope_paths,
            }
        elif action in ("SEARCH_IMAGES", "SEARCH_TABLES"):
            selected_tools = []

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


def _existing_collect_modes(collected_paths: list[dict[str, Any]]) -> dict[str, str]:
    modes: dict[str, str] = {}
    for item in collected_paths:
        path = str(item.get("path") or "")
        if not path:
            continue
        hydrate_mode = str(item.get("hydrate_mode") or "chunks")
        if hydrate_mode != "outline":
            modes[path] = "chunks"
        elif modes.get(path) != "chunks":
            modes[path] = "outline"
    return modes
