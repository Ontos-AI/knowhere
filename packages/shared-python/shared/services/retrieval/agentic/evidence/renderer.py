"""Render agentic document trees into evidence text."""
from __future__ import annotations

from typing import Any, cast

from shared.services.retrieval.agentic.core.types import DocTreeNode


def render_unified_doc_tree(
    node: DocTreeNode,
    doc_name: str,
    depth: int = 0,
    asset_lookup: dict[str, str] | None = None,
) -> str:
    """Render a DocTreeNode as one coherent hierarchy."""
    parts: list[str] = []
    indent = "    " * depth

    if depth == 0:
        parts.append(f"[Document] {doc_name}\n")

    child_prefixes = set(node.children.keys())

    def min_sort(path: str) -> float:
        chunks = node.leaf_content.get(path, [])
        return min((chunk.get("sort_order") or float("inf") for chunk in chunks), default=float("inf"))

    render_queue: list[tuple[float, str, dict | str]] = []
    outline_paths: set[str] = set()
    outline_position = 0.0

    for item in node.outline_items:
        path = item.get("path", "")
        if any(path.startswith(child_prefix + " / ") for child_prefix in child_prefixes):
            continue
        outline_paths.add(path)

        if path in node.leaf_content or path in node.children:
            sort_key = min_sort(path) if path in node.leaf_content else outline_position
        else:
            sort_key = outline_position
        outline_position = max(outline_position, sort_key) + 0.001

        render_queue.append((sort_key, "outline", item))

    for path in node.leaf_content:
        if path not in outline_paths:
            render_queue.append((min_sort(path), "orphan_leaf", path))

    for path in node.children:
        if path not in outline_paths and path not in node.leaf_content:
            child_sort = _infer_child_sort_order(node.children[path])
            render_queue.append((child_sort, "orphan_child", path))

    render_queue.sort(key=lambda item: item[0])

    for _sort_key, render_type, data in render_queue:
        if render_type == "outline":
            item = cast(dict, data)
            path = item.get("path", "")
            title = item.get("title", "")
            is_leaf = item.get("is_leaf", False)
            level = item.get("level", 1)
            leaf_tag = " [Leaf]" if is_leaf else ""

            level_tag = f"[L{level}] " if level else ""
            if level <= 1:
                parts.append(f"{indent}▸ {level_tag}{title}{leaf_tag}")
            else:
                parts.append(f"{indent}└ {level_tag}{title}{leaf_tag}")

            sub_indent = indent + "    "
            if path in node.children:
                child = node.children[path]
                if path in node.leaf_content:
                    render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)
                child_text = render_unified_doc_tree(child, doc_name, depth + 1, asset_lookup=asset_lookup)
                if child_text.strip():
                    parts.append(child_text)
            elif path in node.leaf_content:
                render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)

        elif render_type == "orphan_leaf":
            path = cast(str, data)
            title = path.rsplit(" / ", 1)[-1] if " / " in path else path
            sub_indent = indent + "    "
            if path in node.children:
                # Non-leaf node with own content: render heading, then
                # self chunks, then child subtree (merged rendering).
                parts.append(f"{indent}▸ [L{depth + 1}] {title}")
                render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)
                child_text = render_unified_doc_tree(node.children[path], doc_name, depth + 1, asset_lookup=asset_lookup)
                if child_text.strip():
                    parts.append(child_text)
            else:
                parts.append(f"{indent}▸ [L{depth + 1}] {title} [Leaf]")
                render_leaf_chunks(parts, node.leaf_content[path], sub_indent, asset_lookup=asset_lookup)

        elif render_type == "orphan_child":
            path = cast(str, data)
            title = path.rsplit(" / ", 1)[-1] if " / " in path else path
            child_text = render_unified_doc_tree(node.children[path], doc_name, depth + 1, asset_lookup=asset_lookup)
            # Only render the orphan heading if the child has content.
            # Prevents empty orphan nodes from polluting evidence_text.
            if child_text.strip():
                parts.append(f"{indent}▸ [L{depth + 1}] {title}")
                parts.append(child_text)

    return "\n".join(parts)


def render_leaf_chunks(
    parts: list[str],
    chunks: list[dict[str, Any]],
    indent: str,
    asset_lookup: dict[str, str] | None = None,
) -> None:
    chunk_by_id = {
        chunk.get("chunk_id", ""): chunk
        for chunk in chunks
        if chunk.get("chunk_id")
    }
    rendered_ids: set[str] = set()

    for chunk in chunks:
        chunk_id = chunk.get("chunk_id", "")
        if chunk_id and chunk_id in rendered_ids:
            continue

        chunk_type = (chunk.get("chunk_type") or chunk.get("type") or "text").strip().lower()
        if chunk_type in ("image", "table"):
            continue

        if chunk_id:
            rendered_ids.add(chunk_id)

        content = str(chunk.get("content", "")).strip()
        for connection in (chunk.get("chunk_metadata") or {}).get("connect_to") or []:
            target = chunk_by_id.get(connection.get("target", ""))
            if not target:
                continue
            target_id = target.get("chunk_id", "")
            target_type = (target.get("chunk_type") or target.get("type") or "").strip().lower()
            ref_str = connection.get("ref", "")
            if not ref_str or ref_str not in content:
                continue

            if target_id:
                rendered_ids.add(target_id)

            if target_type == "table":
                table_html = str(target.get("content", "")).strip()
                content = content.replace(ref_str, f"\n[Table]\n{table_html}\n")
            elif target_type == "image":
                file_path = target.get("file_path") or ""
                image_description = str(target.get("content", "")).strip()
                if ref_str in image_description:
                    image_description = image_description.replace(ref_str, "").strip()
                asset_url = (asset_lookup or {}).get(target_id, "") if target_id else ""
                display_ref = asset_url or file_path
                if display_ref:
                    content = content.replace(ref_str, f"\n[Image: {display_ref}]\n{image_description}\n")
                elif image_description:
                    content = content.replace(ref_str, f"\n[Image description]\n{image_description}\n")

        for line in content.split("\n"):
            if line.strip():
                parts.append(f"{indent}┈ {line}")

    for chunk in chunks:
        chunk_id = chunk.get("chunk_id", "")
        if chunk_id and chunk_id in rendered_ids:
            continue
        if chunk_id:
            rendered_ids.add(chunk_id)

        chunk_type = (chunk.get("chunk_type") or chunk.get("type") or "").strip().lower()
        if chunk_type == "image":
            file_path = chunk.get("file_path") or ""
            image_description = str(chunk.get("content", "")).strip()
            asset_url = (asset_lookup or {}).get(chunk_id, "") if chunk_id else ""
            display_ref = asset_url or file_path
            if display_ref:
                parts.append(f"{indent}┈ [Image: {display_ref}]")
            if image_description:
                for line in image_description.split("\n"):
                    if line.strip():
                        parts.append(f"{indent}┈ {line}")
        elif chunk_type == "table":
            table_html = str(chunk.get("content", "")).strip()
            parts.append(f"{indent}┈ [Table]")
            if table_html:
                for line in table_html.split("\n"):
                    if line.strip():
                        parts.append(f"{indent}┈ {line}")


def _infer_child_sort_order(child: DocTreeNode) -> float:
    """Infer sort position from the child's earliest chunk sort_order.

    When an orphan child node has no outline entry, we fall back to the
    minimum ``sort_order`` across all its hydrated chunks so that orphans
    render in document order instead of being appended at the end.
    """
    min_order = float("inf")
    for chunks in child.leaf_content.values():
        for chunk in chunks:
            order = chunk.get("sort_order")
            if order is not None and order < min_order:
                min_order = float(order)
    for grandchild in child.children.values():
        grandchild_order = _infer_child_sort_order(grandchild)
        min_order = min(min_order, grandchild_order)
    return min_order

