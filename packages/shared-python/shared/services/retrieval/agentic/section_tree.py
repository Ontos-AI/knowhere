"""Section-tree loading and prompt projection for agentic navigation."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentSection
from shared.services.retrieval.agentic.section_counts import attach_section_counts
from shared.services.retrieval.lexical_text import normalize_section_path, split_section_path
from shared.utils.text_utils import truncate_content_preview


async def load_child_sections(
    db: AsyncSession,
    document_id: str,
    job_result_id: str,
    scope_path: str | list[str] | None = None,
    exclude_paths: set[str] | None = None,
) -> list[dict]:
    """Load the continuous context tree for a navigation scope."""
    stmt = (
        select(
            DocumentSection.section_id,
            DocumentSection.section_title,
            DocumentSection.section_path,
            DocumentSection.summary,
            DocumentSection.sort_order,
        )
        .where(DocumentSection.document_id == document_id)
        .where(DocumentSection.job_result_id == job_result_id)
        .order_by(DocumentSection.sort_order)
    )
    section_rows = (await db.execute(stmt)).all()
    if not section_rows:
        return []

    if isinstance(scope_path, list):
        scope_list = [normalize_section_path(path) for path in scope_path]
    elif scope_path:
        scope_list = [normalize_section_path(scope_path)]
    else:
        scope_list = []

    scope_depth = len(split_section_path(scope_list[0])) if scope_list else 0
    excluded_paths = exclude_paths or set()

    logger.debug(
        f"  load_child_sections: scopes={scope_list or ['root']} "
        f"scope_depth={scope_depth} exclude_paths={excluded_paths if excluded_paths else 'none'} "
        f"total_sections={len(section_rows)}"
    )

    all_sections: dict[str, dict] = {}
    for section_id, title, path, summary, sort_order in section_rows:
        if not path:
            continue
        normalized_path = normalize_section_path(path)
        parts = split_section_path(normalized_path)
        all_sections[normalized_path] = {
            "title": title or parts[-1] if parts else normalized_path,
            "summary": summary or "",
            "sort_order": int(sort_order or 0),
            "section_id": section_id,
            "parts": parts,
            "depth": len(parts),
        }

    ancestor_prefixes: set[str] = set()
    for scope in scope_list:
        scope_parts = split_section_path(scope)
        for index in range(1, len(scope_parts) + 1):
            ancestor_prefixes.add(" / ".join(scope_parts[:index]))

    items_by_path = _select_scope_items(
        all_sections,
        scope_list=scope_list,
        ancestor_prefixes=ancestor_prefixes,
        exclude_paths=excluded_paths,
    )
    if not items_by_path:
        return []

    allowed_set = _resolve_allowed_depths(items_by_path, scope_list)
    if allowed_set:
        to_remove = [
            path
            for path, item in items_by_path.items()
            if item["show_summary"] and item["level"] not in allowed_set
        ]
        for path in to_remove:
            del items_by_path[path]

    if not items_by_path:
        return []

    await attach_section_counts(
        db,
        document_id=document_id,
        job_result_id=job_result_id,
        all_sections=all_sections,
        items_by_path=items_by_path,
    )

    sorted_items = sorted(items_by_path.values(), key=lambda item: item["sort_order"])
    for item in sorted_items:
        item.pop("sort_order", None)
        item.pop("section_id", None)

    _mark_leaf_and_selectable(sorted_items, all_section_paths=set(all_sections.keys()), allowed_set=allowed_set)
    return sorted_items


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


def _select_scope_items(
    all_sections: dict[str, dict],
    *,
    scope_list: list[str],
    ancestor_prefixes: set[str],
    exclude_paths: set[str],
) -> dict[str, dict]:
    items_by_path: dict[str, dict] = {}

    def is_excluded(path: str) -> bool:
        return bool(
            exclude_paths
            and any(path == excluded or path.startswith(excluded + " / ") for excluded in exclude_paths)
        )

    for path, meta in all_sections.items():
        parts = meta["parts"]
        depth = meta["depth"]

        if not scope_list:
            if depth < 1 or is_excluded(path):
                continue
            items_by_path[path] = _make_item(path, meta, show_summary=True)
            continue

        matched_scope = _find_matched_scope(parts, depth=depth, scope_list=scope_list)
        if matched_scope:
            if is_excluded(path):
                continue
            items_by_path[path] = _make_item(path, meta, show_summary=True)
            continue

        max_scope_depth = max(len(split_section_path(scope)) for scope in scope_list)
        if depth <= max_scope_depth:
            if depth == 1 and path in ancestor_prefixes:
                items_by_path.setdefault(path, _make_item(path, meta, show_summary=False))
            elif depth > 1:
                parent_prefix = " / ".join(parts[:-1])
                if parent_prefix in ancestor_prefixes:
                    items_by_path.setdefault(path, _make_item(path, meta, show_summary=False))

    return items_by_path


def _make_item(path: str, meta: dict, show_summary: bool) -> dict:
    return {
        "path": path,
        "title": meta["title"],
        "summary": meta["summary"],
        "level": meta["depth"],
        "sort_order": meta["sort_order"],
        "chunk_count": 0,
        "image_count": 0,
        "table_count": 0,
        "section_id": meta["section_id"],
        "show_summary": show_summary,
    }


def _find_matched_scope(parts: list[str], *, depth: int, scope_list: list[str]) -> str | None:
    for scope in scope_list:
        scope_parts = split_section_path(scope)
        scope_depth = len(scope_parts)
        if depth > scope_depth and parts[:scope_depth] == scope_parts:
            return scope
    return None


def _resolve_allowed_depths(items_by_path: dict[str, dict], scope_list: list[str]) -> set[int]:
    if not scope_list:
        depths = {
            item["level"]
            for item in items_by_path.values()
            if item.get("show_summary", True)
        }
        return set(sorted(depths)[:2])

    allowed_set: set[int] = set()
    for scope in scope_list:
        scope_parts = split_section_path(scope)
        scope_depth = len(scope_parts)
        child_depths = {
            item["level"]
            for item in items_by_path.values()
            if item.get("show_summary", True)
            and item["level"] > scope_depth
            and split_section_path(item["path"])[:scope_depth] == scope_parts
        }
        if child_depths:
            allowed_set.update(sorted(child_depths)[:2])
    return allowed_set


def _mark_leaf_and_selectable(
    sorted_items: list[dict],
    *,
    all_section_paths: set[str],
    allowed_set: set[int],
) -> None:
    for item in sorted_items:
        item_path = item["path"]
        has_descendants = any(
            path != item_path and path.startswith(item_path + " / ")
            for path in all_section_paths
        )
        item["is_leaf"] = not has_descendants

    if allowed_set:
        shallowest_band = min(allowed_set)
        for item in sorted_items:
            if not item.get("show_summary", True):
                item["selectable"] = False
            elif item["level"] == shallowest_band and not item.get("is_leaf", False):
                item["selectable"] = False
            else:
                item["selectable"] = True
    else:
        for item in sorted_items:
            item["selectable"] = item.get("show_summary", True)


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
