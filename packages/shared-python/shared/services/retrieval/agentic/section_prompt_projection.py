"""Prompt projection for agentic section navigation."""
from __future__ import annotations

from shared.utils.text_utils import truncate_content_preview


def format_items_for_llm(
    items: list[dict],
    max_chars: int = 20000,
) -> tuple[str, bool]:
    """Format section items with hierarchy, selectability, counts, and summaries."""
    if not items:
        return "(no items available)", False

    full_text = "\n".join(_render_item(item, include_summary=True) for item in items)
    if len(full_text) <= max_chars:
        return full_text, False

    slim_text = "\n".join(_render_item(item, include_summary=False) for item in items)
    return slim_text[:max_chars], True


def _render_item(item: dict, include_summary: bool) -> str:
    level = item.get("level", 1)
    show_summary = item.get("show_summary", True)
    is_leaf = item.get("is_leaf", False)
    leaf_tag = " [Leaf]" if is_leaf else ""
    path = item.get("path", "")
    summary = item.get("summary") or ""

    counts_str = ""
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

    indent = "    " * (level - 1)
    prefix = "▸" if level == 1 else "└"
    level_tag = f"[L{level}]"
    select_tag = "[SELECT] " if item.get("selectable", False) else ""

    lines = [
        f'{indent}{prefix} {select_tag}{level_tag} path="{path}"{counts_str}{leaf_tag}'
    ]

    if include_summary and show_summary and summary:
        sub_indent = "    " * level
        clipped = truncate_content_preview(summary, head=80, tail=0)
        lines.append(f"{sub_indent}{clipped}")

    return "\n".join(lines)
