"""Post-navigation discovery selection for agentic retrieval."""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.budget import BudgetExceeded
from shared.services.retrieval.agentic.prompts import (
    DISCOVERY_SELECT_PROMPT,
    format_budget_block,
    parse_action_response,
)
from shared.services.retrieval.agentic.selection_hydration import (
    hydrate_path_selections_into_node,
)
from shared.services.retrieval.agentic.types import DocTreeNode
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.retrieval.llm_adapter import LLMFn


_MAX_DISCOVERY_PER_DOC = 3


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
        await hydrate_path_selections_into_node(
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
