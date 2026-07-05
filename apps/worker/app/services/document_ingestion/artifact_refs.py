from __future__ import annotations

from typing import Any


def collect_referenced_artifact_refs(chunks: list[dict[str, Any]]) -> set[str]:
    """Collect client-visible artifact refs referenced by persisted chunks."""
    refs: set[str] = set()
    for chunk in chunks:
        chunk_type = str(chunk.get("type") or chunk.get("chunk_type") or "").strip().lower()
        metadata = chunk.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        if chunk_type in {"image", "table"}:
            allowed_root = f"{chunk_type}s"
            _add_artifact_ref(
                refs,
                metadata.get("file_path") or chunk.get("file_path") or chunk.get("path"),
                allowed_roots={allowed_root},
            )
        elif chunk_type == "page":
            for page_asset in _iter_page_asset_refs(metadata.get("page_assets")):
                _add_artifact_ref(
                    refs,
                    page_asset,
                    allowed_roots={"page_citation_assets"},
                )
    return refs


def _iter_page_asset_refs(raw_page_assets: object) -> list[object]:
    if not isinstance(raw_page_assets, list):
        return []
    refs: list[object] = []
    for item in raw_page_assets:
        if not isinstance(item, dict):
            continue
        refs.append(item.get("artifact_ref"))
    return refs


def _add_artifact_ref(
    refs: set[str],
    raw_ref: object,
    *,
    allowed_roots: set[str],
) -> None:
    normalized = _normalize_client_artifact_ref(raw_ref)
    if normalized is None:
        return
    root = normalized.split("/", 1)[0]
    if root in allowed_roots:
        refs.add(normalized)


def _normalize_client_artifact_ref(raw_ref: object) -> str | None:
    if raw_ref is None:
        return None
    normalized = str(raw_ref).strip().replace("\\", "/").lstrip("/")
    parts = [
        part
        for part in normalized.split("/")
        if part and part not in {".", ".."}
    ]
    if len(parts) < 2:
        return None
    if parts[0] not in {"images", "tables", "page_citation_assets"}:
        return None
    return "/".join(parts)
