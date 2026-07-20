"""Shared serialization and artifact-writing helpers for the page-memory track."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.page_memory._utils import (
    collapse_page_ranges,
    page_scope_info,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def max_skeleton_end_page(skeletons: list[Any]) -> int:
    return max(
        (int(getattr(item, "end_page", 0) or 0) for item in skeletons),
        default=0,
    )


def derive_hierarchy_page_scope(
    *,
    skeletons: list[Any],
    page_count: int,
) -> list[int]:
    """Pages that should receive PAGE-TAG after hierarchy anchoring."""
    if page_count <= 0:
        return []
    if not skeletons:
        return list(range(1, page_count + 1))

    has_only_root_fallback = all(
        getattr(item, "title", "") == "Root"
        or (getattr(item, "evidence", {}) or {}).get("source") == "fallback_root"
        for item in skeletons
    )
    if has_only_root_fallback:
        return list(range(1, page_count + 1))

    pages: set[int] = set()
    for item in skeletons:
        start_page = max(1, int(getattr(item, "start_page", 1) or 1))
        end_page = min(page_count, int(getattr(item, "end_page", start_page) or start_page))
        if end_page < start_page:
            continue
        pages.update(range(start_page, end_page + 1))
    return sorted(pages) or list(range(1, page_count + 1))


def scope_manifest(
    *,
    scope_id: str,
    skeletons: list[Any],
    page_count: int,
    strategy: str,
) -> dict[str, Any]:
    pages = derive_hierarchy_page_scope(skeletons=skeletons, page_count=page_count)
    parent_paths = sorted({str(getattr(item, "parent_path", "") or "") for item in skeletons})
    return {
        "scope_id": scope_id,
        "strategy": strategy,
        "document_page_count": page_count,
        "page_count": len(pages),
        "page_ranges": collapse_page_ranges(pages),
        "skeleton_count": len(skeletons),
        "parent_paths": parent_paths,
    }


def serialize_skeletons(skeletons: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "section_path": item.section_path,
            "title": item.title,
            "level": item.level,
            "start_page": item.start_page,
            "end_page": item.end_page,
            "parent_path": item.parent_path,
        }
        for item in skeletons
    ]


def build_hierarchy_tree(skeletons: list[Any]) -> dict[str, Any]:
    """Build a nested title tree preserving ``skeletons`` list order.

    Sibling key order follows first-seen order in ``skeletons`` (dict
    insertion order). Callers that need cross-page ordering should
    ``sort_skeletons`` first; same-page order must remain VLM/TOC order.
    """
    hierarchy: dict[str, Any] = {}
    for skel in skeletons:
        parts = str(getattr(skel, "section_path", "") or "").split("/")
        section_parts = parts[1:] if len(parts) > 1 else parts
        current = hierarchy
        for part in section_parts:
            if not part:
                continue
            current = current.setdefault(part, {})
    return hierarchy


def serialize_hierarchy_artifact(
    skeletons: list[Any],
    *,
    scope_manifest_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Keep nodes and HIERARCHY on the same list order — do not re-sort here.
    nodes = serialize_skeletons(skeletons)
    artifact: dict[str, Any] = {
        "HIERARCHY": build_hierarchy_tree(skeletons),
        "nodes": nodes,
        "stats": {
            "node_count": len(nodes),
            **page_scope_info(
                derive_hierarchy_page_scope(
                    skeletons=skeletons,
                    page_count=max_skeleton_end_page(skeletons),
                ),
            ),
            "max_depth": max(
                (int(getattr(item, "level", 0) or 0) for item in skeletons),
                default=0,
            ),
        },
    }
    if scope_manifest_data is not None:
        artifact["scope"] = scope_manifest_data
    return artifact


def serialize_page_tags(tags: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "page_index": item.page_index,
            "summary": item.summary,
            "keywords": list(item.keywords),
            "entities": list(getattr(item, "entities", []) or []),
            "strategy_used": item.strategy_used,
        }
        for item in tags
    ]


def serialize_assets(assets_by_page: dict[int, list[Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page_index in sorted(assets_by_page):
        for asset in assets_by_page[page_index]:
            rows.append(
                {
                    "asset_id": asset.asset_id,
                    "page_index": asset.page_index,
                    "asset_index": asset.asset_index,
                    "kind": asset.kind,
                    "bbox_px": asset.bbox_px,
                    "confidence": asset.confidence,
                    "title": asset.title,
                    "summary": asset.summary,
                    "keywords": list(asset.keywords),
                    "entities": list(getattr(asset, "entities", []) or []),
                    "image_uri": asset.image_uri,
                    "html_uri": asset.html_uri,
                    "extraction_status": asset.extraction_status,
                }
            )
    return rows


def write_scope_artifacts(
    *,
    output_dir: str,
    scope_id: str,
    scope_manifest_data: dict[str, Any],
    hierarchy: list[Any],
    tags: list[Any],
    assets_by_page: dict[int, list[Any]] | None = None,
) -> None:
    scope_dir = Path(output_dir) / "scopes" / scope_id
    write_json(scope_dir / "scope.json", scope_manifest_data)
    write_json(
        scope_dir / "fine_hierarchy.json",
        serialize_hierarchy_artifact(hierarchy, scope_manifest_data=scope_manifest_data),
    )
    write_json(scope_dir / "page_tags.json", serialize_page_tags(tags))
    if assets_by_page:
        write_json(scope_dir / "assets.json", serialize_assets(assets_by_page))


def write_top_level_artifacts(
    *,
    output_dir: str,
    hierarchy: list[Any],
    tags: list[Any],
    assets_by_page: dict[int, list[Any]] | None = None,
) -> None:
    root = Path(output_dir)
    write_json(root / "hierarchy.json", serialize_hierarchy_artifact(hierarchy))
    write_json(root / "page_tags.json", serialize_page_tags(tags))
    if assets_by_page:
        write_json(root / "assets.json", serialize_assets(assets_by_page))
    else:
        (root / "assets.json").unlink(missing_ok=True)
