"""Post-navigation discovery selection for agentic retrieval."""
from __future__ import annotations

import time
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agentic.core.budget import BudgetExceeded
from shared.services.retrieval.agentic.prompts import (
    DISCOVERY_SELECT_PROMPT,
    format_budget_block,
    parse_action_response,
)
from shared.services.retrieval.agentic.navigation.selection_hydration import (
    hydrate_chunk_refs_into_node,
    hydrate_path_selections_into_node,
)
from shared.services.retrieval.agentic.core.types import DocTreeNode
from shared.services.retrieval.search.lexical_text import normalize_section_path
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
    budget_snapshot: dict | None = None,
) -> DocTreeNode:
    """Select and hydrate discovery-found sections after BFS navigation."""
    node = DocTreeNode(scope_path=None)
    if not discovery_hints:
        return node

    hints = discovery_hints[:_MAX_DISCOVERY_PER_DOC]

    t0 = time.monotonic()
    try:
        hint_lines, hint_by_path = _project_discovery_hints(
            hints,
            exclude_paths=exclude_paths,
        )
        if not hint_lines:
            return node

        selections: list[dict[str, Any]] = []
        if hint_lines:
            prompt = _build_discovery_selection_prompt(
                document_id=document_id,
                doc_name=doc_name,
                query=query,
                hint_lines=hint_lines,
                budget_snapshot=budget_snapshot,
            )
            response = await llm_fn(prompt)
            parsed = parse_action_response(response)
            selections = parsed.get("selections", [])

        logger.info(
            f'  discovery_select_step doc="{doc_name}": '
            f"hints={len(hints)} selections={len(selections)}"
        )

        path_selections, chunk_refs = _build_discovery_path_selections(
            selections=selections,
            hint_by_path=hint_by_path,
            document_id=document_id,
            node=node,
        )
        await hydrate_chunk_refs_into_node(
            db,
            node=node,
            refs=chunk_refs,
            user_id=user_id,
            namespace=namespace,
            document_id=document_id,
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
) -> tuple[list[str], dict[str, dict]]:
    exclude_set = {
        normalize_section_path(path)
        for path in (exclude_paths or set())
        if path
    }
    hint_lines: list[str] = []
    hint_by_path: dict[str, dict] = {}
    for hint in hints:
        section_path = normalize_section_path(hint.get("section_path", ""))
        if not section_path:
            continue
        if _is_covered_by_exclude(section_path, exclude_set):
            continue
        if section_path in hint_by_path:
            continue

        hint_by_path[section_path] = hint
        summary = hint.get("summary", "") or ""
        hint_lines.append(f'▸ path="{section_path}"')
        if summary:
            hint_lines.append(f"    {summary[:300]}")

    return hint_lines, hint_by_path


def _is_covered_by_exclude(path: str, exclude_set: set[str]) -> bool:
    """Check if *path* is covered by any entry in *exclude_set*.

    A path is covered if it exactly matches an exclude entry, OR if any
    exclude entry is a prefix of this path (i.e. the parent path was
    already collected by navigation).
    """
    if path in exclude_set:
        return True
    for excluded in exclude_set:
        if path.startswith(excluded + " / "):
            return True
    return False


def _build_discovery_selection_prompt(
    *,
    document_id: str,
    doc_name: str,
    query: str,
    hint_lines: list[str],
    budget_snapshot: dict | None,
) -> str:
    return DISCOVERY_SELECT_PROMPT.format(
        doc_name=doc_name or document_id,
        budget_block=format_budget_block(budget_snapshot),
        items="\n".join(hint_lines),
        query=query,
    )


def _build_discovery_path_selections(
    *,
    selections: list[dict[str, Any]],
    hint_by_path: dict[str, dict],
    document_id: str,
    node: DocTreeNode,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid_selections = [
        selection for selection in selections if selection["path"] in hint_by_path
    ]
    path_selections: list[dict[str, Any]] = []
    chunk_refs: list[dict[str, Any]] = []
    for selection in valid_selections:
        path = selection["path"]
        confidence = selection.get("confidence", 0.7)
        node.confidence[path] = confidence
        hint = hint_by_path[path]
        if path == "Root":
            chunk_id = str(hint.get("chunk_id") or "").strip()
            if chunk_id:
                chunk_refs.append({
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "section_path": path,
                })
                continue
            path_selections.append({
                "path": path,
                "confidence": confidence,
                "hydrate_mode": "self_only",
            })
            continue
        path_selections.append({
            "path": path,
            "confidence": confidence,
            "hydrate_mode": "self_only",
        })

    return path_selections, chunk_refs
