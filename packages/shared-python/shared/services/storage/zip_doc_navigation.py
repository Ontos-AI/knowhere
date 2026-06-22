"""Document navigation projection for Knowhere ZIP result packages."""

from __future__ import annotations

from typing import Any

from shared.services.chunks.document_path import split_document_path
from shared.utils.text_utils import truncate_content_preview


class ZipDocNavigationBuilder:
    def build_hierarchy_dict(
        self,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        hierarchy: dict[str, Any] = {}
        title_counts: dict[str, int] = {}

        for section in sections:
            raw_title = str(section.get("title") or "").strip()
            if not raw_title:
                continue

            title_counts[raw_title] = title_counts.get(raw_title, 0) + 1
            title = (
                raw_title
                if title_counts[raw_title] == 1
                else f"{raw_title} ({title_counts[raw_title]})"
            )
            hierarchy[title] = self.build_hierarchy_dict(
                section.get("children") or []
            )

        return hierarchy

    def build_doc_nav(
        self,
        formatted_chunks: list[dict[str, Any]],
        source_file_name: str,
    ) -> dict[str, Any]:
        text_chunks: list[dict[str, Any]] = []
        table_section_candidates: list[dict[str, Any]] = []
        image_resources: list[dict[str, Any]] = []
        table_resources: list[dict[str, Any]] = []

        stats = {
            "total_chunks": 0,
            "text_chunks": 0,
            "image_chunks": 0,
            "table_chunks": 0,
            "page_chunks": 0,
            "max_depth": 0,
        }

        for formatted_chunk in formatted_chunks:
            chunk_type = formatted_chunk.get("type", "text")
            path = formatted_chunk.get("path", "")
            metadata = formatted_chunk.get("metadata") or {}
            summary_raw = (metadata.get("summary") or "").strip()
            content_raw = (formatted_chunk.get("content") or "").strip()
            summary = " ".join(summary_raw.split()) if summary_raw else ""
            content_preview = truncate_content_preview(content_raw) if content_raw else ""

            stats["total_chunks"] += 1
            if chunk_type == "image":
                stats["image_chunks"] += 1
                image_resources.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )
            elif chunk_type == "table":
                stats["table_chunks"] += 1
                table_resources.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )
                table_section_candidates.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )
            elif chunk_type == "page":
                stats["page_chunks"] += 1
                # Page chunks participate in section tree like text chunks
                text_chunks.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )
            else:
                stats["text_chunks"] += 1
                text_chunks.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )

        section_chunks = text_chunks or table_section_candidates
        sections = self._build_section_tree(
            section_chunks,
            source_file_name=source_file_name,
        )
        stats["max_depth"] = _max_depth(sections)
        return {
            "version": "1.0",
            "file_name": source_file_name or "",
            "stats": stats,
            "sections": sections,
            "resources": {
                "images": image_resources,
                "tables": table_resources,
            },
        }

    def _build_section_tree(
        self,
        text_chunks: list[dict[str, Any]],
        *,
        source_file_name: str,
    ) -> list[dict[str, Any]]:
        root_children: dict[str, dict[str, Any]] = {}

        for chunk in text_chunks:
            path = chunk.get("path", "")
            root_parts, section_parts = split_document_path(
                path,
                source_file_name=source_file_name,
            )

            if not section_parts:
                key = "__root__"
                if key not in root_children:
                    root_children[key] = {
                        "title": "Root",
                        "path": "/".join(root_parts) if root_parts else path,
                        "summary": chunk.get("summary", ""),
                        "chunk_count": 0,
                        "_children_map": {},
                    }
                root_children[key]["chunk_count"] += 1
                if not root_children[key]["summary"]:
                    root_children[key]["summary"] = chunk.get("summary", "")
                continue

            current_level = root_children
            full_section_path_parts = list(root_parts)
            for index, part in enumerate(section_parts):
                full_section_path_parts.append(part)
                if part not in current_level:
                    current_level[part] = {
                        "title": part,
                        "path": "/".join(full_section_path_parts),
                        "summary": "",
                        "chunk_count": 0,
                        "_children_map": {},
                    }
                node = current_level[part]
                if index == len(section_parts) - 1:
                    node["chunk_count"] += 1
                    if not node["summary"]:
                        node["summary"] = chunk.get("summary", "")
                current_level = node["_children_map"]

        return _section_tree_to_output(root_children)


def _max_depth(nodes: list[dict[str, Any]], depth: int = 1) -> int:
    max_depth = depth if nodes else 0
    for node in nodes:
        max_depth = max(max_depth, _max_depth(node.get("children", []), depth + 1))
    return max_depth


def _section_tree_to_output(
    children_map: dict[str, dict[str, Any]],
    level: int = 1,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in children_map.values():
        children = _section_tree_to_output(node["_children_map"], level + 1)
        total_chunks = node["chunk_count"] + sum(
            child.get("chunk_count", 0) for child in children
        )
        result.append(
            {
                "title": node["title"],
                "path": node["path"],
                "level": level,
                "summary": node["summary"],
                "chunk_count": total_chunks,
                "children": children,
            }
        )
    return result


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_doc_nav_from_skeletons(
    skeletons: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    source_file_name: str,
) -> dict[str, Any]:
    """Build doc_nav.json from SectionSkeleton dicts + page chunks.

    Unlike ``ZipDocNavigationBuilder.build_doc_nav`` which infers the tree
    from chunk paths (losing sections whose start page is shared with a
    sibling), this builder uses skeletons as the authoritative tree structure
    and computes chunk_count by page-range overlap.

    Parameters
    ----------
    skeletons:
        List of ``SectionSkeleton.to_dict()`` dicts.  Must have
        ``section_path``, ``title``, ``level``, ``start_page``, ``end_page``,
        and ``parent_path``.
    chunks:
        Formatted chunk dicts (from ``dataframe_to_chunks``).  Only chunks
        with ``type == "page"`` are counted; their ``metadata.page_nums[0]``
        identifies the page index.
    source_file_name:
        Original filename for the doc_nav envelope.
    """
    page_indices: set[int] = set()
    chunk_summaries: dict[int, str] = {}
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        page_nums = meta.get("page_nums") or []
        if page_nums:
            page = _coerce_int(page_nums[0])
            if page is None:
                continue
            page_indices.add(page)
            if not chunk_summaries.get(page):
                chunk_summaries[page] = (
                    (meta.get("summary") or "").strip()
                    or (chunk.get("content") or "").strip()[:200]
                )

    sorted_skels = sorted(
        skeletons,
        key=lambda s: (
            _coerce_int(s.get("start_page")) or 0,
            _coerce_int(s.get("level")) or 0,
            str(s.get("section_path") or ""),
        ),
    )
    nodes: dict[str, dict[str, Any]] = {}
    root_children_order: list[str] = []

    def _link_child(parent_path: str, child_path: str) -> None:
        if not parent_path:
            if child_path not in root_children_order:
                root_children_order.append(child_path)
            return
        parent = nodes.get(parent_path)
        if parent is None:
            return
        children = parent["_children_paths"]
        if child_path not in children:
            children.append(child_path)

    def _ensure_path_node(path: str) -> str:
        root_parts, section_parts = split_document_path(
            path,
            source_file_name=source_file_name,
        )
        if not section_parts:
            return ""

        parent_path = ""
        path_parts = list(root_parts)
        for index, title in enumerate(section_parts):
            path_parts.append(title)
            current_path = "/".join(path_parts)
            if current_path not in nodes:
                nodes[current_path] = {
                    "title": title,
                    "path": current_path,
                    "level": index + 1,
                    "start_page": None,
                    "end_page": None,
                    "summary": "",
                    "parent_path": parent_path,
                    "owned_pages": set(),
                    "_children_paths": [],
                }
            _link_child(parent_path, current_path)
            parent_path = current_path
        return parent_path

    for skel in sorted_skels:
        sp = str(skel.get("section_path") or "").strip()
        if not sp:
            continue
        current_path = _ensure_path_node(sp)
        if not current_path:
            continue

        node = nodes[current_path]
        start_page = _coerce_int(skel.get("start_page"))
        end_page = _coerce_int(skel.get("end_page"))
        if start_page is not None and end_page is not None:
            node["start_page"] = start_page
            node["end_page"] = end_page
            node["owned_pages"].update(page_indices & set(range(start_page, end_page + 1)))
            node["summary"] = chunk_summaries.get(start_page, node["summary"])
        node["title"] = str(skel.get("title") or node["title"])
        node["level"] = _coerce_int(skel.get("level")) or node["level"]

    def _collect_owned_pages(path: str) -> set[int]:
        node = nodes[path]
        pages = set(node["owned_pages"])
        for child_path in node["_children_paths"]:
            pages.update(_collect_owned_pages(child_path))
        node["owned_pages"] = pages
        return pages

    for path in root_children_order:
        if path in nodes:
            _collect_owned_pages(path)

    def _to_output(node: dict[str, Any], level: int = 1) -> dict[str, Any]:
        children_out = [
            _to_output(nodes[child_path], level + 1)
            for child_path in node["_children_paths"]
            if child_path in nodes
        ]
        return {
            "title": node["title"],
            "path": node["path"],
            "level": level,
            "summary": node["summary"],
            "chunk_count": len(node["owned_pages"]),
            "children": children_out,
        }

    sections = [_to_output(nodes[sp]) for sp in root_children_order if sp in nodes]

    # Stats
    stats = {
        "total_chunks": len(chunks),
        "text_chunks": sum(1 for c in chunks if c.get("type") == "text"),
        "image_chunks": sum(1 for c in chunks if c.get("type") == "image"),
        "table_chunks": sum(1 for c in chunks if c.get("type") == "table"),
        "page_chunks": sum(1 for c in chunks if c.get("type") == "page"),
        "max_depth": _max_depth(sections),
    }

    # Resources
    image_resources = []
    table_resources = []
    for chunk in chunks:
        ct = chunk.get("type", "")
        path = chunk.get("path", "")
        meta = chunk.get("metadata") or {}
        summary = (meta.get("summary") or "").strip()
        if ct == "image":
            image_resources.append({"path": path, "summary": summary})
        elif ct == "table":
            table_resources.append({"path": path, "summary": summary})

    return {
        "version": "1.0",
        "file_name": source_file_name or "",
        "stats": stats,
        "sections": sections,
        "resources": {
            "images": image_resources,
            "tables": table_resources,
        },
    }

