"""Schema projection for Knowhere ZIP result packages."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.services.chunks.chunk_connections import (
    build_resource_target_map,
    convert_refs_to_embed_connections,
    merge_connections,
    normalize_connect_to_targets,
    parse_relationship_refs,
)
from shared.services.text_processing.tokenization import truncate_content_preview
from shared.core.time import utc_now_naive


class ZipResultSchemaBuilder:
    def calculate_statistics(self, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        total_chunks = len(chunks)
        text_chunks = 0
        image_chunks = 0
        table_chunks = 0

        for chunk in chunks:
            chunk_type = chunk.get("type", "")
            raw_type = str(chunk_type).strip()
            normalized_type = raw_type.split("\n", 1)[0].lower()
            if normalized_type == "image":
                image_chunks += 1
            elif normalized_type == "table":
                table_chunks += 1
            else:
                text_chunks += 1

        return {
            "total_chunks": total_chunks,
            "text_chunks": text_chunks,
            "image_chunks": image_chunks,
            "table_chunks": table_chunks,
            "total_pages": None,
        }

    def format_chunks(
        self,
        chunks: List[Dict[str, Any]],
        image_files_map: Dict[str, Dict[str, Any]],
        table_files_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        resource_target_map = build_resource_target_map(
            chunks,
            image_files_map=image_files_map,
            table_files_map=table_files_map,
        )

        formatted = []
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or chunk.get("know_id"))
            chunk_type_str = chunk.get("type", "")
            raw_type = str(chunk_type_str).strip()
            normalized_type = raw_type.split("\n", 1)[0].lower()
            img_info = image_files_map.get(chunk_id)

            if normalized_type == "image":
                chunk_type = "image"
            elif normalized_type == "table":
                chunk_type = "table"
            else:
                chunk_type = "text"

            content = chunk.get("text") or chunk.get("content", "")
            path = chunk.get("path", "")
            existing_metadata = chunk.get("metadata", {})
            metadata = {
                "length": existing_metadata.get("length") or len(content),
                "summary": existing_metadata.get("summary") or chunk.get("summary", ""),
                "page_nums": existing_metadata.get("page_nums", []),
            }
            document_top_summary = str(
                existing_metadata.get("document_top_summary") or ""
            ).strip()
            if document_top_summary:
                metadata["document_top_summary"] = document_top_summary

            if chunk_type == "text":
                metadata["tokens"] = existing_metadata.get("tokens") or chunk.get(
                    "tokens", 0
                )
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                relationship_refs = parse_relationship_refs(
                    chunk.get("type_raw") or chunk_type_str,
                    str(content),
                )
                embed_connections = convert_refs_to_embed_connections(
                    relationship_refs, resource_target_map
                )
                related_connections = normalize_connect_to_targets(
                    existing_metadata.get("connect_to")
                    or chunk.get("connect_to")
                    or chunk.get("connectto"),
                    resource_target_map,
                )
                metadata["connect_to"] = merge_connections(
                    embed_connections, related_connections
                )

            elif chunk_type == "image":
                if img_info:
                    metadata["file_path"] = img_info["file_path"]
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []

            elif chunk_type == "table":
                file_path = existing_metadata.get("file_path")
                if not file_path:
                    table_info = table_files_map.get(chunk_id)
                    if table_info:
                        file_path = table_info["file_path"]
                    else:
                        table_name = (
                            path.split("/")[-1]
                            if "/" in path
                            else f"table_{chunk_id}.html"
                        )
                        file_path = f"tables/{table_name}"

                metadata["file_path"] = file_path
                metadata["keywords"] = existing_metadata.get("keywords") or chunk.get(
                    "keywords", []
                )
                metadata["tokens"] = []

            formatted.append(
                {
                    "chunk_id": chunk_id,
                    "type": chunk_type,
                    "content": content,
                    "path": path,
                    "metadata": metadata,
                }
            )

        return formatted

    def generate_manifest(
        self,
        *,
        job_id: str,
        data_id: Optional[str],
        source_file_name: str,
        statistics: Dict[str, Any],
        job_metadata: Dict[str, Any],
        hierarchy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "version": "2.0",
            "job_id": job_id,
            "data_id": data_id,
            "source_file_name": source_file_name,
            "processing_date": utc_now_naive().isoformat() + "Z",
            "processing": {
                "page_count": job_metadata.get("page_count"),
                "billing_status": job_metadata.get("billing_status"),
                "cost": {
                    "micro_dollars": job_metadata.get("billing_amount_micro_dollars"),
                    "credits": job_metadata.get("billing_credits"),
                },
                "timing": {
                    "started_at": job_metadata.get("processing_started_at"),
                    "completed_at": job_metadata.get("processing_completed_at"),
                    "duration_ms": job_metadata.get("processing_duration_ms"),
                },
            },
            "statistics": statistics,
            "HIERARCHY": hierarchy or {},
        }

    def build_hierarchy_dict(
        self,
        sections: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        hierarchy: Dict[str, Any] = {}
        title_counts: Dict[str, int] = {}

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
        formatted_chunks: List[Dict[str, Any]],
        source_file_name: str,
    ) -> Dict[str, Any]:
        text_chunks: List[Dict[str, Any]] = []
        image_resources: List[Dict[str, Any]] = []
        table_resources: List[Dict[str, Any]] = []

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

        sections = self._build_section_tree(text_chunks)
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
        text_chunks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        root_children: Dict[str, dict] = {}

        for chunk in text_chunks:
            path = chunk.get("path", "")
            parts = [part.strip() for part in path.split("/") if part.strip()]
            section_parts = parts[2:] if len(parts) > 2 else []

            if not section_parts:
                key = "__root__"
                if key not in root_children:
                    root_children[key] = {
                        "title": "Root",
                        "path": "/".join(parts[:2]) if len(parts) >= 2 else path,
                        "summary": chunk.get("summary", ""),
                        "chunk_count": 0,
                        "_children_map": {},
                    }
                root_children[key]["chunk_count"] += 1
                if not root_children[key]["summary"]:
                    root_children[key]["summary"] = chunk.get("summary", "")
                continue

            current_level = root_children
            full_section_path_parts = parts[:2]
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


def _max_depth(nodes: list, depth: int = 1) -> int:
    max_depth = depth if nodes else 0
    for node in nodes:
        max_depth = max(max_depth, _max_depth(node.get("children", []), depth + 1))
    return max_depth


def _section_tree_to_output(
    children_map: Dict[str, dict],
    level: int = 1,
) -> List[Dict[str, Any]]:
    result = []
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
