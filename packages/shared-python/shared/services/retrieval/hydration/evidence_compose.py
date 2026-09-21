"""Compose retrieval evidence parts from raw chunks and connected assets.

Text and standalone image/table chunks place table HTML and image bytes at
path placeholders. Page chunks are summary plus the page image; they do not
inline connected charts.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from shared.services.retrieval.hydration.asset_inline import (
    remove_path_placeholders,
)
from shared.services.retrieval.hydration.row_utils import (
    extract_page_nums,
    normalize_chunk_type,
    page_summary,
)
from shared.services.retrieval.hydration.table_grid import (
    TableDownloadError,
    load_table_html,
)
from shared.services.storage.result_storage import get_result_storage

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def compose_evidence_parts(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    chunk_type = normalize_chunk_type(row.get("chunk_type"))
    if chunk_type == "page":
        return _compose_page_parts(row)
    if chunk_type == "table":
        return _compose_standalone_table_parts(row)
    if chunk_type == "image":
        return _compose_standalone_image_parts(row)
    return _compose_text_parts(row, rows_by_chunk_id)


def collect_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for row in rows:
        composed = row.get("composed")
        if isinstance(composed, list):
            parts.extend(composed)
    return parts


def flatten_parts(parts: list[dict[str, Any]] | None) -> str:
    texts: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = str(part.get("text") or "")
            if text:
                texts.append(text)
            continue
        if part.get("type") != "image":
            continue
        media_type = str(part.get("media_type") or "").strip() or "application/octet-stream"
        data = str(part.get("data") or "").strip()
        if data:
            texts.append(f"data:{media_type};base64,{data}")
    return "".join(texts)


def _compose_page_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    summary = page_summary(row)
    if summary:
        parts.append(_text_part(summary))
    image, warning = _try_read_page_image(row)
    if image is not None:
        parts.append(image)
    if warning:
        parts.append(_text_part(f"Page image unavailable: {warning}"))
    return parts


def _compose_standalone_table_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    html = _try_read_table_html(row)
    if html is None:
        return []
    return [_text_part(f"\n{html}\n")]


def _compose_standalone_image_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    description = str(row.get("content") or "").strip()
    if description:
        parts.append(_text_part(description))
    image = _try_read_image(row)
    if image is not None:
        parts.append(image)
    return parts


def _compose_text_parts(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    content = str(row.get("content") or "")
    tables, images = _embed_targets(row, rows_by_chunk_id)
    for _target_id, target_row, ref in tables:
        html = _try_read_table_html(target_row)
        content, placed = _replace_placeholder(content, ref, "" if html is None else f"\n{html}\n")
        if html is not None and not placed:
            _warn_skipped(target_row, "table", "placeholder not found")
    parts: list[dict[str, Any]] = []
    remaining = content
    unused = list(images)
    while unused:
        match = _earliest_image_placeholder(remaining, unused)
        if match is None:
            _warn_skipped(unused[0][1], "image", "placeholder not found")
            unused.pop(0)
            continue
        before, after, target_row = match
        if before:
            parts.append(_text_part(before))
        image = _try_read_image(target_row)
        if image is not None:
            if parts and parts[-1]["type"] == "text":
                parts[-1]["text"] += "\n"
            else:
                parts.append(_text_part("\n"))
            parts.append(image)
            parts.append(_text_part("\n"))
        remaining = after
        unused = [item for item in unused if item[1] is not target_row]
    if remaining:
        parts.append(_text_part(remaining))
    cleaned: list[dict[str, Any]] = []
    for part in parts:
        cleaned_part = _clean_text_part(part)
        if not _is_empty_text_part(cleaned_part):
            cleaned.append(cleaned_part)
    return cleaned


def _embed_targets(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> tuple[
    list[tuple[str, dict[str, Any], str]],
    list[tuple[str, dict[str, Any], str]],
]:
    tables: list[tuple[str, dict[str, Any], str]] = []
    images: list[tuple[str, dict[str, Any], str]] = []
    for item in _connections(row):
        if item.get("relation") != "embeds":
            continue
        target_id = str(item.get("target") or "").strip()
        ref = str(item.get("ref") or "").strip()
        target_row = rows_by_chunk_id.get(target_id)
        if not target_id or not ref or target_row is None:
            continue
        target_type = normalize_chunk_type(target_row.get("chunk_type"))
        if target_type == "table":
            tables.append((target_id, target_row, ref))
        elif target_type == "image":
            images.append((target_id, target_row, ref))
    return tables, images


def _connections(row: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    connections = metadata.get("connect_to") or []
    if not isinstance(connections, list):
        return []
    return [item for item in connections if isinstance(item, dict)]


def _replace_placeholder(text: str, ref: str, replacement: str) -> tuple[str, bool]:
    for candidate in _ref_candidates(ref):
        if candidate and candidate in text:
            return text.replace(candidate, replacement, 1), True
    return text, False


def _earliest_image_placeholder(
    text: str,
    images: list[tuple[str, dict[str, Any], str]],
) -> tuple[str, str, dict[str, Any]] | None:
    best: tuple[int, int, dict[str, Any]] | None = None
    for _target_id, target_row, ref in images:
        for candidate in _ref_candidates(ref):
            if not candidate:
                continue
            index = text.find(candidate)
            if index < 0:
                continue
            length = len(candidate)
            if (
                best is None
                or index < best[0]
                or (index == best[0] and length > best[1])
            ):
                best = (index, length, target_row)
    if best is None:
        return None
    index, length, target_row = best
    return text[:index], text[index + length :], target_row


def _ref_candidates(ref: str) -> list[str]:
    raw = str(ref or "").strip()
    if not raw:
        return []
    out = [raw]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner and inner not in out:
            out.append(inner)
    else:
        bracketed = f"[{raw}]"
        if bracketed not in out:
            out.append(bracketed)
    return out


def _try_read_table_html(row: dict[str, Any]) -> str | None:
    try:
        html = load_table_html(row).strip()
    except TableDownloadError as exc:
        _warn_skipped(row, "table", str(exc))
        return None
    if html:
        return html
    _warn_skipped(row, "table", "missing table HTML")
    return None


def _try_read_image(row: dict[str, Any]) -> dict[str, Any] | None:
    artifact = str(row.get("file_path") or "").strip()
    return _try_read_image_artifact(row, artifact, media_type=_media_type_from_path(artifact))


def _try_read_page_image(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    asset, warning = _select_page_asset(row)
    if asset is None:
        _warn_skipped(row, "image", warning)
        return None, warning
    artifact = str(asset.get("artifact_ref") or "").strip()
    media_type = (
        str(asset.get("content_type") or "").split(";", 1)[0].strip()
        or _media_type_from_path(artifact)
    )
    image = _try_read_image_artifact(row, artifact, media_type=media_type)
    if image is None:
        return None, "could not read page image"
    return image, None


def _try_read_image_artifact(
    row: dict[str, Any],
    artifact: str,
    *,
    media_type: str,
) -> dict[str, Any] | None:
    job_id = str(row.get("job_id") or "").strip()
    storage = get_result_storage()
    normalized = storage.normalize_artifact_ref(artifact)
    if not job_id or not normalized:
        _warn_skipped(row, "image", "missing artifact")
        return None
    try:
        temp_path = storage.download_raw_to_temp(
            job_id=job_id,
            relative_path=normalized,
            suffix=Path(normalized).suffix or ".bin",
            temp_dir=tempfile.gettempdir(),
        )
        body = Path(temp_path).read_bytes()
    except Exception as exc:
        _warn_skipped(row, "image", str(exc))
        return None
    if not body:
        _warn_skipped(row, "image", "empty image bytes")
        return None
    return {
        "type": "image",
        "media_type": media_type,
        "data": base64.b64encode(body).decode("ascii"),
    }


def _select_page_asset(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None, "missing page image"
    assets = metadata.get("page_assets") or []
    if not isinstance(assets, list):
        return None, "missing page image"
    candidates = [item for item in assets if isinstance(item, dict)]
    if not candidates:
        return None, "missing page image"
    page_nums = extract_page_nums(row) or []
    if not page_nums:
        return None, "missing page number"
    for item in candidates:
        try:
            page_num = int(item.get("page_num"))
        except (TypeError, ValueError):
            continue
        if page_num in page_nums:
            return item, None
    return None, "page image does not match this page"


def _media_type_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _IMAGE_MEDIA_TYPES.get(suffix, "application/octet-stream")


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _clean_text_part(part: dict[str, Any]) -> dict[str, Any]:
    if part.get("type") != "text":
        return part
    return {"type": "text", "text": remove_path_placeholders(str(part.get("text") or ""))}


def _is_empty_text_part(part: dict[str, Any]) -> bool:
    return part.get("type") == "text" and not str(part.get("text") or "")


def _warn_skipped(row: dict[str, Any], kind: str, reason: str) -> None:
    logger.warning(
        "retrieval: skipped unreachable evidence asset type={} ref={} asset_url={} source_path={} reason={}",
        kind,
        row.get("chunk_id"),
        row.get("asset_url"),
        row.get("file_path"),
        reason,
    )
