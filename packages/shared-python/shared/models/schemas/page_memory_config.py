"""Page-memory worker configuration captured in job metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Self


@dataclass(frozen=True)
class PageMemoryConfig:
    """Resolved page-memory defaults used by worker execution."""

    max_pages: int = 1500
    scope_concurrency: int = 5
    tag_concurrency: int = 4
    title_detection_concurrency: int = 3
    node_assembly_concurrency: int = 3
    tag_mode: Literal["vlm", "text"] = "vlm"
    fine_min_pages: int = 4
    hierarchy_model: str | None = None
    hierarchy_max_tokens: int = 2000
    max_heading_depth: int = 6
    asset_extraction_enabled: bool = False
    asset_summary_enabled: bool = False
    asset_model: str = "qwen3.6-flash"
    asset_max_pages: int | None = None
    asset_confidence_threshold: float = 0.3
    asset_summary_concurrency: int = 4
    table_engine: str = "tabula"
    table_merge_enabled: bool = True
    node_summary_max_pages: int = 5
    page_locate_residual_agent_limit: int = 50
    page_locate_max_emit_depth: int = 5
    page_locate_min_emit_depth: int = 2
    page_locate_vlm_candidate_page_cap: int = 4
    page_locate_full_leaf_sections: bool = False
    scan_direction: str = "top_to_bottom_left_to_right"

    @classmethod
    def default(cls) -> Self:
        from shared.core.config import settings

        return cls(
            scope_concurrency=_as_int(
                getattr(settings, "PAGE_MEMORY_SCOPE_CONCURRENCY", 5),
                5,
            ),
            tag_concurrency=_as_int(
                getattr(settings, "PAGE_MEMORY_TAG_CONCURRENCY", 4),
                4,
            ),
            title_detection_concurrency=_as_int(
                getattr(settings, "PAGE_MEMORY_TITLE_DETECTION_CONCURRENCY", 3),
                3,
            ),
            node_assembly_concurrency=_as_int(
                getattr(settings, "PAGE_MEMORY_NODE_ASSEMBLY_CONCURRENCY", 3),
                3,
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        if not isinstance(value, dict):
            return cls.default()

        default = cls.default()
        tag_mode = str(value.get("tag_mode", default.tag_mode)).strip().lower()
        resolved_tag_mode: Literal["vlm", "text"] = (
            "text" if tag_mode == "text" else "vlm"
        )
        return cls(
            max_pages=_as_int(value.get("max_pages"), default.max_pages),
            scope_concurrency=_as_int(
                value.get("scope_concurrency"),
                default.scope_concurrency,
            ),
            tag_concurrency=_as_int(
                value.get("tag_concurrency"),
                default.tag_concurrency,
            ),
            title_detection_concurrency=_as_int(
                value.get("title_detection_concurrency"),
                default.title_detection_concurrency,
            ),
            node_assembly_concurrency=_as_int(
                value.get("node_assembly_concurrency"),
                default.node_assembly_concurrency,
            ),
            tag_mode=resolved_tag_mode,
            fine_min_pages=_as_int(
                value.get("fine_min_pages"),
                default.fine_min_pages,
            ),
            hierarchy_model=_as_optional_str(value.get("hierarchy_model")),
            hierarchy_max_tokens=_as_int(
                value.get("hierarchy_max_tokens"),
                default.hierarchy_max_tokens,
            ),
            max_heading_depth=_as_int(
                value.get("max_heading_depth"),
                default.max_heading_depth,
            ),
            asset_extraction_enabled=_as_bool(
                value.get("asset_extraction_enabled"),
                default.asset_extraction_enabled,
            ),
            asset_summary_enabled=_as_bool(
                value.get("asset_summary_enabled"),
                default.asset_summary_enabled,
            ),
            asset_model=_as_str(value.get("asset_model"), default.asset_model),
            asset_max_pages=_as_optional_int(value.get("asset_max_pages")),
            asset_confidence_threshold=_as_float(
                value.get("asset_confidence_threshold"),
                default.asset_confidence_threshold,
            ),
            asset_summary_concurrency=_as_int(
                value.get("asset_summary_concurrency"),
                default.asset_summary_concurrency,
            ),
            table_engine=_as_str(value.get("table_engine"), default.table_engine),
            table_merge_enabled=_as_bool(
                value.get("table_merge_enabled"),
                default.table_merge_enabled,
            ),
            node_summary_max_pages=_as_int(
                value.get("node_summary_max_pages"),
                default.node_summary_max_pages,
            ),
            page_locate_residual_agent_limit=_as_int(
                value.get("page_locate_residual_agent_limit"),
                default.page_locate_residual_agent_limit,
            ),
            page_locate_max_emit_depth=_as_int(
                value.get("page_locate_max_emit_depth"),
                default.page_locate_max_emit_depth,
            ),
            page_locate_min_emit_depth=_as_int(
                value.get("page_locate_min_emit_depth"),
                default.page_locate_min_emit_depth,
            ),
            page_locate_vlm_candidate_page_cap=_as_int(
                value.get("page_locate_vlm_candidate_page_cap"),
                default.page_locate_vlm_candidate_page_cap,
            ),
            page_locate_full_leaf_sections=_as_bool(
                value.get("page_locate_full_leaf_sections"),
                default.page_locate_full_leaf_sections,
            ),
            scan_direction=_as_str(
                value.get("scan_direction"), default.scan_direction
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _as_int(value: object, default: int) -> int:
    try:
        resolved = int(str(value))
    except (TypeError, ValueError):
        return default
    return resolved if resolved > 0 else default


def _as_optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        resolved = int(str(value))
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _as_float(value: object, default: float) -> float:
    try:
        resolved = float(str(value))
    except (TypeError, ValueError):
        return default
    return resolved


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_str(value: object, default: str) -> str:
    if value is None:
        return default
    resolved = str(value).strip()
    return resolved or default


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    resolved = str(value).strip()
    return resolved or None
