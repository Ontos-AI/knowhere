"""Asset descriptors shared by extracted assets and page-level embedded media.

Image and table chunks describe themselves with ``metadata.asset``. Page rows
carry the page's video and audio as ``metadata.assets`` plus one marker line
per item in their content, so text retrieval and agents see the media too.
Media bytes live under the job's result prefix in ``embedded_media/`` and are
reached through signed URLs only.
"""

from __future__ import annotations

import mimetypes
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

PAGE_MEDIA_ASSETS_KEY: Final = "assets"
HIDDEN_PAGE_NUMS_KEY: Final = "hidden_page_nums"
EXTRACTED_ASSET_KEY: Final = "asset"
EMBEDDED_MEDIA_DIR: Final = "embedded_media"
PAGE_MEDIA_KINDS: Final = frozenset({"video", "audio"})
_PAGE_MEDIA_ORIGINS: Final = frozenset({"embedded", "linked"})
_EXTRACTED_ASSET_KINDS: Final = frozenset({"image", "table"})
_OPTIONAL_TEXT_KEYS: Final = ("title", "summary", "content_type", "checksum_sha256")
_OPTIONAL_COUNT_KEYS: Final = ("size_bytes", "duration_ms")


def embedded_media_path(value: object) -> str | None:
    """Return a normalized ``embedded_media/<name>`` path, or None when outside it."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        return None
    normalized = posixpath.normpath(value)
    parts = normalized.split("/")
    if len(parts) < 2 or parts[0] != EMBEDDED_MEDIA_DIR or ".." in parts:
        return None
    return normalized


def normalize_page_media_asset(value: object) -> dict[str, Any] | None:
    """Keep a well-formed video/audio descriptor; drop anything else."""
    if not isinstance(value, Mapping):
        return None
    asset_id = value.get("asset_id")
    kind = value.get("kind")
    origin = value.get("origin")
    page_nums = _page_numbers(value.get("page_nums"))
    ref = _normalize_ref(value.get("ref"))
    if (
        not isinstance(asset_id, str)
        or not asset_id
        or kind not in PAGE_MEDIA_KINDS
        or origin not in _PAGE_MEDIA_ORIGINS
        or not page_nums
        or ref is None
    ):
        return None
    descriptor: dict[str, Any] = {
        "asset_id": asset_id,
        "kind": kind,
        "origin": origin,
        "page_nums": page_nums,
        "region": _normalize_region(value.get("region")),
        "ref": ref,
    }
    for key in _OPTIONAL_TEXT_KEYS:
        text = value.get(key)
        descriptor[key] = text if isinstance(text, str) and text else None
    for key in _OPTIONAL_COUNT_KEYS:
        count = value.get(key)
        descriptor[key] = count if _is_positive_int(count) else None
    return descriptor


def normalize_page_media_assets(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        descriptor
        for item in value
        if (descriptor := normalize_page_media_asset(item)) is not None
    ]


def normalize_hidden_page_nums(value: object) -> list[int]:
    return _page_numbers(value)


def page_media_marker(asset: Mapping[str, Any]) -> str:
    """Language-neutral marker naming the media and its asset id."""
    parts: list[str] = []
    if asset.get("title"):
        parts.append(str(asset["title"]))
    if asset.get("origin") == "linked":
        parts.append("linked")
    elif _is_positive_int(asset.get("duration_ms")):
        parts.append(_format_duration(int(asset["duration_ms"])))
    parts.append(f"asset:{asset['asset_id']}")
    return f"[{asset['kind']}: {' · '.join(parts)}]"


@dataclass(frozen=True)
class PageMedia:
    """A document's embedded media and hidden pages, keyed by page number."""

    assets_by_page: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    hidden_pages: frozenset[int] = frozenset()

    @classmethod
    def from_descriptors(
        cls, assets: Iterable[object], hidden_page_nums: Iterable[int]
    ) -> PageMedia:
        assets_by_page: dict[int, list[dict[str, Any]]] = {}
        seen: set[tuple[int, str]] = set()
        for item in assets:
            descriptor = normalize_page_media_asset(item)
            if descriptor is None:
                continue
            for page in descriptor["page_nums"]:
                key = (page, descriptor["asset_id"])
                if key not in seen:
                    seen.add(key)
                    assets_by_page.setdefault(page, []).append(descriptor)
        return cls(
            assets_by_page=assets_by_page,
            hidden_pages=frozenset(normalize_hidden_page_nums(list(hidden_page_nums))),
        )

    @classmethod
    def from_page_metadatas(cls, metadatas: Iterable[object]) -> PageMedia:
        """Collect what earlier page rows of the same revision carried."""
        assets: list[object] = []
        hidden: list[int] = []
        for metadata in metadatas:
            if not isinstance(metadata, Mapping):
                continue
            assets.extend(normalize_page_media_assets(metadata.get(PAGE_MEDIA_ASSETS_KEY)))
            hidden.extend(normalize_hidden_page_nums(metadata.get(HIDDEN_PAGE_NUMS_KEY)))
        return cls.from_descriptors(assets, hidden)

    @property
    def is_empty(self) -> bool:
        return not self.assets_by_page and not self.hidden_pages

    def attach(self, *, content: str, metadata: dict[str, Any], pages: Iterable[int]) -> str:
        """Set the page row's media fields when absent; return content with markers."""
        page_list = list(pages)
        if PAGE_MEDIA_ASSETS_KEY not in metadata:
            assets = self._assets_for_pages(page_list)
            if assets:
                metadata[PAGE_MEDIA_ASSETS_KEY] = assets
        if HIDDEN_PAGE_NUMS_KEY not in metadata:
            hidden = sorted(page for page in set(page_list) if page in self.hidden_pages)
            if hidden:
                metadata[HIDDEN_PAGE_NUMS_KEY] = hidden
        markers = [
            page_media_marker(asset)
            for asset in normalize_page_media_assets(metadata.get(PAGE_MEDIA_ASSETS_KEY))
            if f"asset:{asset['asset_id']}" not in content
        ]
        if not markers:
            return content
        return "\n".join(part for part in (content.rstrip(), *markers) if part)

    def _assets_for_pages(self, pages: list[int]) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in pages:
            for asset in self.assets_by_page.get(page, []):
                if asset["asset_id"] not in seen:
                    seen.add(asset["asset_id"])
                    assets.append(dict(asset))
        return assets


def describe_extracted_asset(
    *, chunk_id: str, chunk_type: str, metadata: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Describe an image or table chunk with the shared descriptor fields."""
    file_path = metadata.get("file_path")
    if chunk_type not in _EXTRACTED_ASSET_KINDS or not chunk_id or not file_path:
        return None
    title = metadata.get("asset_title")
    summary = metadata.get("summary")
    return {
        "asset_id": chunk_id,
        "kind": chunk_type,
        "origin": "extracted",
        "title": title if isinstance(title, str) and title else None,
        "summary": summary if isinstance(summary, str) and summary else None,
        "page_nums": _page_numbers(metadata.get("page_nums")),
        "region": _extracted_region(metadata.get("asset_region")),
        "content_type": mimetypes.guess_type(str(file_path))[0],
        "size_bytes": None,
        "checksum_sha256": None,
        "duration_ms": None,
        "ref": {"kind": "artifact", "path": str(file_path)},
    }


def _extracted_region(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    region = _normalize_region(value)
    if region is None:
        return None
    for key in ("page_width", "page_height"):
        if _is_positive_int(value.get(key)):
            region[key] = value[key]
    return region


def _normalize_region(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    page_num = value.get("page_num")
    bbox = value.get("bbox")
    coord_system = value.get("coord_system")
    if (
        not _is_positive_int(page_num)
        or not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(item, (int, float)) for item in bbox)
        or not isinstance(coord_system, str)
    ):
        return None
    return {"page_num": page_num, "bbox": list(bbox), "coord_system": coord_system}


def _normalize_ref(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("kind") == "artifact":
        path = embedded_media_path(value.get("path"))
        return {"kind": "artifact", "path": path} if path else None
    url = value.get("url")
    if value.get("kind") == "external" and isinstance(url, str) and url:
        return {"kind": "external", "url": url}
    return None


def _page_numbers(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if _is_positive_int(item)]


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _format_duration(duration_ms: int) -> str:
    total_seconds = duration_ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
