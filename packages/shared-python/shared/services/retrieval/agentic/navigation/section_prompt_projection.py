"""Prompt projection for agentic section navigation (Collector Agent model)."""
from __future__ import annotations

from typing import Any

from shared.utils.text_utils import truncate_content_preview


def format_items_for_llm(
    items: list[dict],
    max_chars: int = 20000,
    collected_paths: set[str] | None = None,
) -> tuple[str, bool]:
    """Format section items with hierarchy, token estimates, and collection marks."""
    if not items:
        return "(no items available)", False

    coll = collected_paths or set()
    full_text = "\n".join(_render_item(item, include_summary=True, collected=coll) for item in items)
    if len(full_text) <= max_chars:
        return full_text, False

    slim_text = "\n".join(_render_item(item, include_summary=False, collected=coll) for item in items)
    return slim_text[:max_chars], True


def _render_item(item: dict, include_summary: bool, collected: set[str]) -> str:
    level = item.get("level", 1)
    show_summary = item.get("show_summary", True)
    is_leaf = item.get("is_leaf", False)
    path = item.get("path", "")
    summary = item.get("summary") or ""

    # Check if this path (or an ancestor) is already collected
    is_collected = _is_path_collected(path, collected)
    collected_tag = "[✓] " if is_collected else ""

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
        f'{indent}{prefix} {collected_tag}{level_tag} path="{path}"{counts_str}{token_str}{leaf_tag}'
    ]

    if include_summary and show_summary and summary:
        sub_indent = "    " * level
        clipped = truncate_content_preview(summary, head=80, tail=0)
        lines.append(f"{sub_indent}{clipped}")

    return "\n".join(lines)


def _is_path_collected(path: str, collected: set[str]) -> bool:
    """Check if path itself or any ancestor is in the collected set."""
    if path in collected:
        return True
    for coll_path in collected:
        if path.startswith(coll_path + " / "):
            return True
    return False


def format_collection_status(
    collected_paths: list[dict[str, Any]],
) -> str:
    """Render the collection status block for the navigation prompt."""
    if not collected_paths:
        return ""

    lines = [f"=== Collection Status ({len(collected_paths)} items) ==="]
    for item in collected_paths:
        path = item.get("path", "")
        conf = item.get("confidence", 0)
        step = item.get("collected_at_step", "?")
        outline = item.get("outline", False)
        mode_tag = " [outline]" if outline else ""
        lines.append(f'✓ "{path}" (step {step}, conf={conf:.1f}{mode_tag})')
    lines.append("=== End Collection ===")
    return "\n".join(lines)


def format_nav_trace(
    nav_trace: list[dict[str, Any]],
    collected_paths: list[dict[str, Any]],
) -> str:
    """Render the unified navigation trace block (includes scope, actions, and collection)."""
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
        if action == "DRILL" and drill_into:
            action_display = f'DRILL "{drill_into}"'

        lines.append(f"Step {step}: scope={scope} → {action_display}")

        # Show what was collected in this step
        step_collected = entry.get("collected", [])
        if step_collected:
            paths_display = ", ".join(f'"{c}"' for c in step_collected)
            lines.append(f"  collected: {paths_display}")

        if reason:
            lines.append(f"  reason: {reason}")
        lines.append("")

    # Append current collection summary
    if collected_paths:
        total = len(collected_paths)
        lines.append(f"[Current] collection: {total} items")
        lines.append("Do NOT re-collect paths marked [✓] below.")

    lines.append("=== End Trace ===")
    return "\n".join(lines)
