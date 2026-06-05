"""Prompt projection for agentic section navigation (Collector Agent model)."""
from __future__ import annotations

from typing import Any

from shared.utils.text_utils import truncate_content_preview


def format_items_for_llm(
    items: list[dict],
    max_chars: int = 20000,
    collected_paths: set[str] | None = None,
    expanded_paths: set[str] | None = None,
) -> tuple[str, bool]:
    """Format section items with hierarchy, token estimates, and collection marks."""
    if not items:
        return "(no items available)", False

    coll = collected_paths or set()
    expanded = expanded_paths or set()
    full_text = "\n".join(
        _render_item(
            item,
            include_summary=True,
            collected=coll,
            expanded=expanded,
        )
        for item in items
    )
    if len(full_text) <= max_chars:
        return full_text, False

    slim_text = "\n".join(
        _render_item(
            item,
            include_summary=False,
            collected=coll,
            expanded=expanded,
        )
        for item in items
    )
    return slim_text[:max_chars], True


def _render_item(
    item: dict,
    include_summary: bool,
    collected: set[str],
    expanded: set[str],
) -> str:
    level = item.get("level", 1)
    show_summary = item.get("show_summary", True)
    is_leaf = item.get("is_leaf", False)
    path = item.get("path", "")
    summary = item.get("summary") or ""

    # Check if this path (or an ancestor) is already collected
    is_collected = _is_path_collected(path, collected)
    collected_tag = "[✓] " if is_collected else ""
    expanded_tag = "[seen] " if not is_collected and path in expanded else ""

    leaf_tag = " [Leaf]" if is_leaf else ""

    # Counts and token estimate
    counts_str = ""
    token_str = ""
    if show_summary:
        count_parts: list[str] = []
        chunk_count = item.get("chunk_count", 0)
        if chunk_count > 0:
            count_parts.append(f"text={chunk_count}")
        image_count = item.get("image_count", 0)
        if image_count > 0:
            count_parts.append(f"image={image_count}")
        table_count = item.get("table_count", 0)
        if table_count > 0:
            count_parts.append(f"table={table_count}")
        counts_str = f'  [{" ".join(count_parts)}]' if count_parts else ""

        total_chars = item.get("total_chars", 0)
        if total_chars > 0:
            # Approximate tokens: Chinese ~2 chars/token, English ~4 chars/token
            # Use conservative 2 chars/token for mixed content
            tokens = total_chars / 2
            if tokens >= 1000:
                token_str = f" ~{tokens / 1000:.1f}k tokens"
            else:
                token_str = f" ~{int(tokens)} tokens"

    indent = "    " * (level - 1)
    prefix = "▸" if level == 1 else "└"
    level_tag = f"[L{level}]"

    lines = [
        f'{indent}{prefix} {collected_tag}{expanded_tag}{level_tag} path="{path}"{counts_str}{token_str}{leaf_tag}'
    ]

    if include_summary and show_summary and summary:
        sub_indent = "    " * level
        display_summary = _enrich_section_covers_summary(summary)
        clipped = truncate_content_preview(display_summary, head=80, tail=0)
        lines.append(f"{sub_indent}{clipped}")

    return "\n".join(lines)


def _enrich_section_covers_summary(summary: str) -> str:
    """Inject sub-section count into 'This section covers:' summaries.

    Transforms:
        'This section covers: A, B, C'
    into:
        'This section covers 3 sub-sections: A, B, C'
    """
    prefix = "This section covers: "
    if not summary.startswith(prefix):
        return summary
    body = summary[len(prefix):]
    sub_sections = [s.strip() for s in body.split(", ") if s.strip()]
    count = len(sub_sections)
    return f"This section covers {count} sub-sections: {body}"


def _is_path_collected(path: str, collected: set[str]) -> bool:
    """Check if path itself or any ancestor is in the collected set."""
    if path in collected:
        return True
    for coll_path in collected:
        if path.startswith(coll_path + " / "):
            return True
    return False


def format_nav_trace(
    nav_trace: list[dict[str, Any]],
    collected_paths: list[dict[str, Any]],
) -> str:
    """Render the unified navigation trace block.

    Includes navigation history and collected paths with modes.
    """
    if not nav_trace and not collected_paths:
        return ""

    lines = ["=== Navigation Trace ==="]
    for entry in nav_trace:
        step = entry.get("step", "?")
        scope = entry.get("scope", "root")
        action = entry.get("action", "?")
        reason = entry.get("reason", "")
        action_display = action
        drill_into = entry.get("drill_into")
        if action == "EXPAND" and drill_into:
            action_display = f'EXPAND "{drill_into}"'
        elif action == "BACK":
            back_to = entry.get("back_to")
            target = f'"{back_to}"' if back_to else "root"
            action_display = f"BACK to {target}"

        lines.append(f"Step {step}: scope={scope} → {action_display}")

        # Show tool usage and results so LLM can avoid repeating searches
        tool_results = entry.get("tool_results", {})
        if tool_results:
            tool_name = tool_results.get("tool", "")
            tool_query = tool_results.get("query", "")
            matched = tool_results.get("matched", False)
            status = "found matches" if matched else "no matches"
            lines.append(f'  🔧 {tool_name}("{tool_query}") → {status}')

        # Show what was collected in this step
        step_collected = entry.get("collected", [])
        if step_collected:
            paths_display = ", ".join(f'"{c}"' for c in step_collected)
            lines.append(f"  collected: {paths_display}")

        result_status = entry.get("result_status")
        if result_status and result_status != "ok":
            lines.append(f"  result_status: {result_status}")

        if reason:
            lines.append(f"  reason: {reason}")
        lines.append("")

    # Collection summary with per-item details
    if collected_paths:
        lines.append(f"[Current] collection: {len(collected_paths)} items")
        for item in collected_paths:
            path = item.get("path", "")
            is_outline = (
                item.get("hydrate_mode") == "outline"
                or item.get("outline", False)
            )
            mode_tag = " [outline]" if is_outline else ""
            step_num = item.get("collected_at_step", "?")
            lines.append(f'  ✓ "{path}"{mode_tag} (step {step_num})')
        lines.append("Do NOT re-collect these paths or paths marked [✓] in the tree.")

    lines.append("=== End Trace ===")
    return "\n".join(lines)


def format_main_actions_block(current_scope: str | None) -> str:
    """Render only actions valid for the current navigation scope."""
    lines = [
        "    - EXPAND — Observe a visible unprocessed section's children in the next step.",
        "      Use for larger relevant sections when child-level selection is needed.",
        "      action_args.target must be an exact visible path that is not [seen], [✓],",
        "      the current scope, or an ancestor of the current scope.",
        "    - SEARCH_IMAGES — Ask the asset-inspector sub-agent to inspect images.",
        "      Choose only when the Asset actions block above lists SEARCH_IMAGES.",
        "      Requires action_args.query.",
        "    - SEARCH_TABLES — Ask the asset-inspector sub-agent to inspect tables.",
        "      Choose only when the Asset actions block above lists SEARCH_TABLES.",
        "      Requires action_args.query.",
    ]
    if current_scope:
        targets = _format_back_targets(current_scope)
        lines.extend([
            "    - BACK — Move to an ancestor scope to explore other branches.",
            f"      action_args.target must be one of: {targets}",
            "      Prefer the nearest relevant ancestor; use null only to return to root.",
        ])
    lines.extend([
        "    - FINISH — End navigation for this document when enough evidence is collected",
        "      or no unprocessed relevant section remains.",
    ])
    return "\n".join(lines)


def _format_back_targets(current_scope: str) -> str:
    parts = current_scope.split(" / ")
    targets: list[str] = []
    for i in range(len(parts) - 1, 0, -1):
        targets.append(f'"{" / ".join(parts[:i])}"')
    targets.append("null (root)")
    return ", ".join(targets)
