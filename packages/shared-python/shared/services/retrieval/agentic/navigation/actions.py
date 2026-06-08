"""Legal action projection for agentic document navigation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from shared.services.retrieval.agentic.core.budget import budget_status_from_snapshot
from shared.services.retrieval.agentic.navigation.path_ledger import PathLedger
from shared.services.retrieval.search.lexical_text import normalize_section_path
from shared.utils.text_utils import truncate_content_preview


ActionKind = Literal[
    "EXPAND",
    "COLLECT",
    "BACK",
    "SEARCH_IMAGES",
    "SEARCH_TABLES",
    "FINISH",
]


@dataclass(frozen=True)
class LegalAction:
    id: str
    action: ActionKind
    path: str | None = None
    target_scope: str | None = None
    asset_type: str | None = None
    note: str | None = None
    source: str = "tree"
    score: float = 0.0
    critical_expand: bool = False


@dataclass
class LegalActionSet:
    by_id: dict[str, LegalAction] = field(default_factory=dict)
    expand: list[LegalAction] = field(default_factory=list)
    collect: list[LegalAction] = field(default_factory=list)
    back: list[LegalAction] = field(default_factory=list)
    search: list[LegalAction] = field(default_factory=list)
    finish: LegalAction | None = None

    def add(self, action: LegalAction) -> None:
        self.by_id[action.id] = action
        if action.action == "EXPAND":
            self.expand.append(action)
        elif action.action == "COLLECT":
            self.collect.append(action)
        elif action.action == "BACK":
            self.back.append(action)
        elif action.action in ("SEARCH_IMAGES", "SEARCH_TABLES"):
            self.search.append(action)
        elif action.action == "FINISH":
            self.finish = action

    def get(self, action_id: str | None) -> LegalAction | None:
        if not action_id:
            return None
        return self.by_id.get(action_id)


def build_legal_actions(
    *,
    items: list[dict[str, Any]],
    current_scope: str | None,
    collected_paths: list[dict[str, Any]],
    expanded_scopes: set[str],
    discovery_hints: list[dict[str, Any]] | None = None,
    rejected_paths: set[str] | None = None,
    rejected_collect_paths: set[str] | None = None,
    total_images: int,
    total_tables: int,
    disabled_asset_types: set[str] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
) -> LegalActionSet:
    action_set = LegalActionSet()
    covered_paths = _covered_paths(collected_paths)
    outline_paths = _outline_paths(collected_paths)
    budget_mode = budget_status_from_snapshot(budget_snapshot)
    rejected = {PathLedger.normalize(path) for path in rejected_paths or set()}
    rejected_collects = {
        normalized
        for path in rejected_collect_paths or set()
        if (normalized := PathLedger.normalize(path))
    }
    discovery_scores = _discovery_scores_by_path(discovery_hints or [])
    scored_items = _score_items(items, discovery_scores)
    ranked_items = _rank_items(scored_items)
    expand_allowlist = _expand_allowlist(
        ranked_items,
        budget_mode=budget_mode,
        limit=3,
    )

    expand_index = 1
    collect_index = 1
    for item in scored_items:
        path = str(item.get("path") or "").strip()
        if not path or path == "Root":
            continue
        if PathLedger.is_covered(path, covered_paths):
            continue
        if PathLedger.is_covered(path, rejected_collects):
            continue

        action_set.add(LegalAction(
            id=f"C{collect_index}",
            action="COLLECT",
            path=path,
            target_scope=path,
            note=(
                "upgrade outline to full evidence"
                if path in outline_paths
                else _item_note(item)
            ),
            score=float(item.get("relevance_score") or 0.0),
        ))
        collect_index += 1

        critical_expand = False
        if budget_mode == "EXHAUSTED":
            continue
        if budget_mode == "CRITICAL":
            if action_set.collect:
                continue
            critical_expand = True
        if item.get("is_leaf"):
            continue
        if path == current_scope:
            continue
        if current_scope and PathLedger.is_ancestor(path, current_scope):
            continue
        if path in expanded_scopes:
            continue
        if (
            budget_mode == "TIGHT"
            and path not in expand_allowlist
        ):
            continue
        if path in rejected and not _path_has_discovery_signal(path, discovery_scores):
            continue

        action_set.add(LegalAction(
            id=f"E{expand_index}",
            action="EXPAND",
            path=path,
            target_scope=path,
            note=_item_note(item),
            score=float(item.get("relevance_score") or 0.0),
            critical_expand=critical_expand,
        ))
        expand_index += 1

    discovery_index = 1
    seen_discovery_paths: set[str] = set()
    for hint in sorted(
        discovery_hints or [],
        key=lambda item: float(item.get("discovery_score") or 0.0),
        reverse=True,
    ):
        path = normalize_section_path(str(hint.get("section_path") or ""))
        if not path or path in seen_discovery_paths:
            continue
        seen_discovery_paths.add(path)
        if PathLedger.is_covered(path, covered_paths):
            continue
        # TODO: allow tool-specific LLM adjudicators to revive rejected
        # collects when validity cannot be determined structurally.
        if PathLedger.is_covered(path, rejected_collects):
            continue
        if any(action.path == path for action in action_set.collect):
            continue
        action_set.add(LegalAction(
            id=f"D{discovery_index}",
            action="COLLECT",
            path=path,
            target_scope=path,
            note=_discovery_note(hint),
            source="discovery",
            score=float(hint.get("discovery_score") or 0.0),
        ))
        discovery_index += 1

    search_allowed = budget_mode not in ("CRITICAL", "EXHAUSTED")
    disabled_assets = {item.lower() for item in disabled_asset_types or set()}
    if (
        search_allowed
        and "image" not in disabled_assets
        and total_images > 0
        and _asset_search_worthwhile(ranked_items, "image")
    ):
        action_set.add(LegalAction(
            id="S1",
            action="SEARCH_IMAGES",
            asset_type="image",
            note=f"{total_images} images available in current scope",
        ))
    if (
        search_allowed
        and "table" not in disabled_assets
        and total_tables > 0
        and _asset_search_worthwhile(ranked_items, "table")
    ):
        action_set.add(LegalAction(
            id="S2",
            action="SEARCH_TABLES",
            asset_type="table",
            note=f"{total_tables} tables available in current scope",
        ))

    if current_scope and budget_mode != "EXHAUSTED":
        back_index = 1
        for target in PathLedger.back_targets(current_scope):
            label = target if target is not None else "root"
            action_set.add(LegalAction(
                id=f"B{back_index}",
                action="BACK",
                path=target,
                target_scope=target,
                note=f"return to {label}",
            ))
            back_index += 1

    action_set.add(LegalAction(
        id="F1",
        action="FINISH",
        note="finish this document",
    ))
    return action_set


def format_agent_state_block(
    *,
    current_scope: str | None,
    query_intent: str,
    expanded_scopes: set[str],
    rejected_paths: set[str],
    collected_paths: list[dict[str, Any]],
    rejected_collect_paths: set[str] | None = None,
    prior_tool_result: dict[str, Any] | None,
    search_context: str,
    budget_snapshot: dict[str, Any] | None,
) -> str:
    lines = [
        "=== Agent State ===",
        f"Current scope: {current_scope or 'root'}",
        f"Advisory query intent: {query_intent or 'UNKNOWN'}",
        _format_budget_state(budget_snapshot),
    ]
    budget_mode = budget_status_from_snapshot(budget_snapshot)
    if budget_mode == "CRITICAL":
        lines.append(
            "Budget policy: exploration actions are closed; collect the best visible "
            "evidence before FINISH."
        )
    elif budget_mode == "EXHAUSTED":
        lines.append(
            "Budget policy: planning budget is exhausted or in overdraft. Do not "
            "explore or search again. Use the current observation and tool results "
            "to decide FINISH, or collect only indispensable visible evidence."
        )
    if expanded_scopes:
        lines.append("Expanded scopes:")
        for path in sorted(expanded_scopes):
            lines.append(f'  - "{path}"')
    else:
        lines.append("Expanded scopes: none")
    rejected_collects = set(rejected_collect_paths or set())
    low_value_rejected = set(rejected_paths) - rejected_collects
    if low_value_rejected:
        lines.append("Low-value scopes avoided unless revived by discovery:")
        for path in sorted(low_value_rejected):
            lines.append(f'  - "{path}"')
    if rejected_collect_paths:
        lines.append("Collects rejected by tool reconciliation:")
        for path in sorted(rejected_collect_paths):
            lines.append(f'  - "{path}"')

    full_paths, outline_paths = _dedupe_collection_modes(collected_paths)
    if full_paths or outline_paths:
        if full_paths:
            lines.append(f"Full evidence collected: {len(full_paths)} item(s)")
            for path in full_paths:
                lines.append(f'  - "{path}"')
        else:
            lines.append("Full evidence collected: none")
        if outline_paths:
            lines.append(
                f"Outline-only evidence: {len(outline_paths)} item(s) "
                "(structure only; not hydrated as full chunks)"
            )
            for path in outline_paths:
                lines.append(f'  - "{path}"')
    else:
        lines.append("Collected evidence: none")

    if prior_tool_result:
        lines.append(f"Last tool result: {_compact_dict(prior_tool_result)}")
    if search_context:
        lines.append("Tool observation:")
        lines.append(search_context.strip())
    lines.append("=== End Agent State ===")
    return "\n".join(lines)


def format_actionable_observation(
    *,
    items: list[dict[str, Any]],
    action_set: LegalActionSet,
    max_chars: int = 20000,
) -> tuple[str, bool]:
    """Render visible document state and legal action affordances once."""
    if not items:
        return "(no visible sections)", False

    full_text = _render_actionable_items(
        items=items,
        action_set=action_set,
        include_summary=True,
    )
    if len(full_text) <= max_chars:
        return full_text, False

    slim_text = _render_actionable_items(
        items=items,
        action_set=action_set,
        include_summary=False,
    )
    return slim_text[:max_chars], True


def _render_actionable_items(
    *,
    items: list[dict[str, Any]],
    action_set: LegalActionSet,
    include_summary: bool,
) -> str:
    collect_by_path = {
        action.path: action
        for action in action_set.collect
        if action.path
    }
    expand_by_path = {
        action.path: action
        for action in action_set.expand
        if action.path
    }
    lines = [
        "=== Actionable Observation ===",
        "Each visible section appears once. Choose action IDs attached to the relevant line.",
    ]
    for item in items:
        lines.extend(_render_actionable_item(
            item=item,
            collect_action=collect_by_path.get(str(item.get("path") or "")),
            expand_action=expand_by_path.get(str(item.get("path") or "")),
            include_summary=include_summary,
        ))

    discovery_lines = _format_discovery_actions(action_set)
    if discovery_lines:
        lines.append("")
        lines.append("Discovery hints:")
        lines.extend(discovery_lines)

    global_actions = _format_global_actions(action_set)
    if global_actions:
        lines.append("")
        lines.append("Global actions:")
        lines.extend(global_actions)
    lines.append("=== End Actionable Observation ===")
    return "\n".join(lines)


def _render_actionable_item(
    *,
    item: dict[str, Any],
    collect_action: LegalAction | None,
    expand_action: LegalAction | None,
    include_summary: bool,
) -> list[str]:
    level = int(item.get("level", 1) or 1)
    show_summary = bool(item.get("show_summary", True))
    has_actions = collect_action is not None or expand_action is not None
    show_details = show_summary or has_actions
    path = str(item.get("path") or "")
    summary = str(item.get("summary") or "")
    is_leaf = bool(item.get("is_leaf", False))
    indent = "    " * max(level - 1, 0)
    prefix = "▸" if level == 1 else "└"
    level_tag = f"depth={level}"
    counts = _format_counts(item) if show_details else ""
    tokens = _format_token_estimate(item) if show_details else ""
    leaf = " [Leaf]" if is_leaf else ""
    actions = _format_node_actions(
        collect_action=collect_action,
        expand_action=expand_action,
    )

    lines = [
        f'{indent}{prefix} {level_tag} path="{path}"{counts}{tokens}{leaf} actions: {actions}'
    ]
    if include_summary and show_details and summary:
        display_summary = _enrich_section_covers_summary(summary)
        clipped = truncate_content_preview(display_summary, head=120, tail=0)
        lines.append(f"{indent}    summary: {clipped}")
    return lines


def _format_node_actions(
    *,
    collect_action: LegalAction | None,
    expand_action: LegalAction | None,
) -> str:
    actions: list[str] = []
    if collect_action:
        collect_name = (
            "collect_full"
            if collect_action.note == "upgrade outline to full evidence"
            else "collect"
        )
        actions.append(f"{collect_name}={collect_action.id}")
    if expand_action:
        actions.append(f"expand={expand_action.id}")
    return ", ".join(actions) if actions else "none"


def _format_discovery_actions(action_set: LegalActionSet) -> list[str]:
    lines: list[str] = []
    for action in action_set.collect:
        if action.source != "discovery" or not action.path:
            continue
        note = f" | {action.note}" if action.note else ""
        lines.append(f'  {action.id} -> "{action.path}"{note}')
    return lines


def _format_global_actions(action_set: LegalActionSet) -> list[str]:
    lines: list[str] = []
    for action in action_set.search:
        if action.action == "SEARCH_IMAGES":
            lines.append(f"  search_images={action.id} ({action.note})")
        elif action.action == "SEARCH_TABLES":
            lines.append(f"  search_tables={action.id} ({action.note})")
    for action in action_set.back:
        target = action.target_scope or "root"
        lines.append(f"  back={action.id} -> {target}")
    if action_set.finish:
        lines.append(f"  finish={action_set.finish.id}")
    return lines


def _format_counts(item: dict[str, Any]) -> str:
    parts: list[str] = []
    chunk_count = int(item.get("chunk_count") or 0)
    image_count = int(item.get("image_count") or 0)
    table_count = int(item.get("table_count") or 0)
    if chunk_count:
        parts.append(f"text={chunk_count}")
    if image_count:
        parts.append(f"image={image_count}")
    if table_count:
        parts.append(f"table={table_count}")
    return f'  [{" ".join(parts)}]' if parts else ""


def _format_token_estimate(item: dict[str, Any]) -> str:
    total_chars = int(item.get("total_chars") or 0)
    if total_chars <= 0:
        return ""
    tokens = total_chars / 2
    if tokens >= 1000:
        return f" ~{tokens / 1000:.1f}k tokens"
    return f" ~{int(tokens)} tokens"


def _enrich_section_covers_summary(summary: str) -> str:
    prefix = "This section covers: "
    if not summary.startswith(prefix):
        return summary
    body = summary[len(prefix):]
    sub_sections = [s.strip() for s in body.split(", ") if s.strip()]
    return f"This section covers {len(sub_sections)} sub-sections: {body}"


def _covered_paths(collected_paths: list[dict[str, Any]]) -> set[str]:
    return {
        PathLedger.normalize(str(item.get("path") or ""))
        for item in collected_paths
        if item.get("path") and item.get("hydrate_mode") != "outline"
    }


def _outline_paths(collected_paths: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("path") or "")
        for item in collected_paths
        if item.get("path") and item.get("hydrate_mode") == "outline"
    }


def _dedupe_collection_modes(
    collected_paths: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    full: set[str] = set()
    outline: set[str] = set()
    for item in collected_paths:
        path = str(item.get("path") or "")
        if not path:
            continue
        if item.get("hydrate_mode") == "outline":
            outline.add(path)
        else:
            full.add(path)
    outline -= full
    return sorted(full), sorted(outline)


def _discovery_note(hint: dict[str, Any]) -> str | None:
    summary = str(hint.get("summary") or "").strip()
    score = float(hint.get("discovery_score") or 0.0)
    score_note = f"score={score:.2f}" if score > 0 else ""
    if summary:
        clipped = truncate_content_preview(summary, head=120, tail=0)
        return f"{clipped} {score_note}".strip()
    chunk_type = str(hint.get("chunk_type") or "").strip()
    if chunk_type:
        return f"bottom-discovery hit type={chunk_type} {score_note}".strip()
    return f"bottom-discovery hit {score_note}".strip()


def _format_budget_state(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return "Budget mode: UNKNOWN"
    planning = snapshot.get("planning")
    if not isinstance(planning, dict):
        return "Budget mode: UNKNOWN"
    status = str(planning.get("status") or "UNKNOWN")
    used_pct = planning.get("used_pct")
    remaining = planning.get("remaining")
    capacity = planning.get("capacity")
    overdraft = int(planning.get("overdraft") or 0)
    overdraft_note = f", overdraft={overdraft}" if overdraft > 0 else ""
    if used_pct is None:
        return f"Budget mode: {status}"
    if remaining is not None and capacity:
        return (
            f"Budget mode: {status} ({used_pct}% used, "
            f"{remaining}/{capacity} tokens remaining{overdraft_note})"
        )
    return f"Budget mode: {status} ({used_pct}% used{overdraft_note})"


def _item_note(item: dict[str, Any]) -> str | None:
    parts: list[str] = []
    chunk_count = int(item.get("chunk_count") or 0)
    image_count = int(item.get("image_count") or 0)
    table_count = int(item.get("table_count") or 0)
    if chunk_count:
        parts.append(f"text={chunk_count}")
    if image_count:
        parts.append(f"image={image_count}")
    if table_count:
        parts.append(f"table={table_count}")
    score = float(item.get("relevance_score") or 0.0)
    if score > 0:
        parts.append(f"relevance={score:.2f}")
    if item.get("is_leaf"):
        parts.append("leaf")
    return " ".join(parts) if parts else None


def _discovery_scores_by_path(
    discovery_hints: list[dict[str, Any]],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for hint in discovery_hints:
        path = normalize_section_path(str(hint.get("section_path") or ""))
        if not path:
            continue
        score = float(hint.get("discovery_score") or 0.0)
        scores[path] = max(scores.get(path, 0.0), score)
    return scores


def _score_items(
    items: list[dict[str, Any]],
    discovery_scores: dict[str, float],
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        copied = dict(item)
        copied["_original_index"] = index
        copied["relevance_score"] = _score_item(copied, discovery_scores)
        scored.append(copied)
    return scored


def _rank_items(scored_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        scored_items,
        key=lambda item: (
            float(item.get("relevance_score") or 0.0),
            int(item.get("chunk_count") or 0),
            int(item.get("table_count") or 0) + int(item.get("image_count") or 0),
            -int(item.get("_original_index") or 0),
        ),
        reverse=True,
    )


def _score_item(
    item: dict[str, Any],
    discovery_scores: dict[str, float],
) -> float:
    path = normalize_section_path(str(item.get("path") or ""))
    if not path:
        return 0.0
    score = discovery_scores.get(path, 0.0)
    for hint_path, hint_score in discovery_scores.items():
        if PathLedger.is_ancestor(path, hint_path):
            score = max(score, float(hint_score) * 0.9)
        elif PathLedger.is_ancestor(hint_path, path):
            score = max(score, float(hint_score) * 0.65)
    return min(score, 1.0)


def _expand_allowlist(
    ranked_items: list[dict[str, Any]],
    *,
    budget_mode: str,
    limit: int,
) -> set[str]:
    if budget_mode != "TIGHT":
        return {
            normalize_section_path(str(item.get("path") or ""))
            for item in ranked_items
            if item.get("path")
        }
    candidates = [
        normalize_section_path(str(item.get("path") or ""))
        for item in ranked_items
        if item.get("path")
        and not item.get("is_leaf")
    ]
    return set(candidates[:limit])


def _path_has_discovery_signal(
    path: str,
    discovery_scores: dict[str, float],
) -> bool:
    return any(
        candidate == path
        or PathLedger.is_ancestor(path, candidate)
        or PathLedger.is_ancestor(candidate, path)
        for candidate in discovery_scores
    )


def _asset_search_worthwhile(
    ranked_items: list[dict[str, Any]],
    asset_kind: Literal["image", "table"],
) -> bool:
    count_key = "image_count" if asset_kind == "image" else "table_count"
    return any(int(item.get(count_key) or 0) > 0 for item in ranked_items[:5])


def _compact_dict(value: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("tool", "status", "matched", "candidate_count", "status_detail"):
        if key in value:
            bits.append(f"{key}={value[key]}")
    budget = value.get("budget")
    if isinstance(budget, dict):
        delta = budget.get("delta")
        after = budget.get("after")
        if isinstance(delta, dict):
            bits.append(
                "budget_delta="
                f"used:{delta.get('used', 0)}, "
                f"used_pct:{delta.get('used_pct', 0)}, "
                f"overdraft:{delta.get('overdraft', 0)}"
            )
        if isinstance(after, dict) and int(after.get("overdraft") or 0) > 0:
            bits.append(f"budget_overdraft={after.get('overdraft')}")
    return ", ".join(bits) if bits else str(value)
