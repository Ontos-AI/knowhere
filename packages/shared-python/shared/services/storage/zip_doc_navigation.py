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
        image_resources: list[dict[str, Any]] = []
        table_resources: list[dict[str, Any]] = []

        stats = {
            "total_chunks": 0,
            "text_chunks": 0,
            "image_chunks": 0,
            "table_chunks": 0,
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
            else:
                stats["text_chunks"] += 1
                text_chunks.append(
                    {
                        "path": path,
                        "summary": summary or content_preview,
                    }
                )

        sections = self._build_section_tree(
            text_chunks,
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
