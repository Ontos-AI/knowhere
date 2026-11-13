"""Pluggable Fine-stage skills for page-memory (atlas drawing sheets first)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol, Sequence

from loguru import logger

from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory

DRAWING_SHEET_SKILL_NAME = "drawing_sheet"


class FineSkill(Protocol):
    """Fine-stage addon: prompt text, extra tag fields, and optional C5 override."""

    name: str
    extra_tag_fields: dict[str, str]

    def title_prompt_addon(self) -> str:
        """Optional addon for outline title detection. Empty when unused."""
        ...

    def page_tag_prompt_addon(self) -> str:
        """Addon appended to ``page-memory-vlm-tag``."""
        ...

    def node_summary_prompt_addon(self) -> str:
        """Addon appended to ``page-memory-node-summary``."""
        ...

    def override_page_assets(
        self,
        *,
        page: Any,
        tag: Any | None,
        output_dir: str,
        source_name: str,
    ) -> list[Any] | None:
        """Return assets to skip VLM detect/crop, or ``None`` for the standard path."""
        ...


class DrawingSheetFineSkill:
    """Per-page drawing-sheet skill: name/metadata in tagging, intact page image in C5."""

    name = DRAWING_SHEET_SKILL_NAME
    extra_tag_fields = {
        "is_drawing_sheet": "boolean",
        "drawing_title": "string",
    }

    def title_prompt_addon(self) -> str:
        return ""

    def page_tag_prompt_addon(self) -> str:
        return (
            "Additional JSON keys (required in addition to the keys above):\n"
            "{\n"
            '  "is_drawing_sheet": true|false,\n'
            '  "drawing_title": "<drawing_no + drawing_name from title block, or empty>"\n'
            "}\n"
            "First check this page, and apply drawing rules only when it is an "
            "engineering/technical drawing sheet.\n"
            '- "is_drawing_sheet": true only when the page is primarily an '
            "architectural/engineering/CAD drawing, schematic, or detail sheet. "
            "false for covers, body specifications, pure schedules/tables, or TOC.\n"
            '- "drawing_title": the drawing number and drawing name from the title '
            "block / info bar when present; otherwise an empty string.\n"
            "When is_drawing_sheet is true: prefer the corner/edge title block "
            "(drawing number, drawing name, project/atlas name, scale, discipline, "
            "revision) and technical notes on the sheet when forming summary and "
            "entities.\n"
            "When is_drawing_sheet is false: follow the standard rules only."
        )

    def node_summary_prompt_addon(self) -> str:
        return (
            "If the provided page image(s) are engineering/technical drawing sheets, "
            "prefer the corner/edge title block (drawing number, drawing name, "
            "project/atlas name, scale, discipline, revision) and technical notes "
            "on the sheet when summarizing this section."
        )

    def override_page_assets(
        self,
        *,
        page: Any,
        tag: Any | None,
        output_dir: str,
        source_name: str,
    ) -> list[Any] | None:
        signals = dict(getattr(tag, "skill_signals", None) or {})
        if signals.get("is_drawing_sheet") is not True:
            return None

        from app.services.document_parser.assets.asset_file_names import (
            ASSET_KIND_IMAGE,
            build_page_asset_file_stem,
        )
        from app.services.document_parser.support.identifiers import gen_str_codes
        from app.services.page_memory.page_assets import PageAsset
        from PIL import Image

        image_path = str(getattr(page, "image_path", "") or "")
        page_index = int(getattr(page, "page_index", 0) or 0)
        if not image_path or not Path(image_path).is_file():
            logger.warning(
                "[fine_skills] drawing sheet page {} missing render; skip assets",
                page_index,
            )
            return []

        try:
            with Image.open(image_path) as image:
                width_px, height_px = image.size
        except Exception as exc:
            logger.warning(
                "[fine_skills] failed to read drawing sheet page {}: {}",
                page_index,
                exc,
            )
            return []

        drawing_title = str(signals.get("drawing_title") or "").strip()
        stem = build_page_asset_file_stem(
            ASSET_KIND_IMAGE,
            page_index,
            1,
            drawing_title,
        )
        image_dir = Path(output_dir) / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{stem}.png"
        dest = image_dir / filename
        try:
            shutil.copy2(image_path, dest)
        except Exception as exc:
            logger.warning(
                "[fine_skills] failed to copy intact drawing page {}: {}",
                page_index,
                exc,
            )
            return []

        bbox_px = [0, 0, int(width_px), int(height_px)]
        asset_id = "asset_" + gen_str_codes(
            f"{source_name}::{page_index}::1::figure::{bbox_px}::{drawing_title}"
        )
        keywords = list(getattr(tag, "keywords", None) or [])
        entities = list(getattr(tag, "entities", None) or [])
        summary = str(getattr(tag, "summary", "") or "").strip()
        return [
            PageAsset(
                asset_id=asset_id,
                page_index=page_index,
                asset_index=1,
                kind="figure",
                bbox_px=bbox_px,
                width_px=int(width_px),
                height_px=int(height_px),
                width_pt=float(getattr(page, "width", 0.0) or 0.0),
                height_pt=float(getattr(page, "height", 0.0) or 0.0),
                title=drawing_title,
                summary=summary,
                keywords=keywords,
                entities=entities,
                image_uri=f"images/{filename}",
                image_path=str(dest),
                extraction_status="intact",
            )
        ]


_REGISTERED_FINE_SKILLS: dict[str, FineSkill] = {
    DRAWING_SHEET_SKILL_NAME: DrawingSheetFineSkill(),
}


def _normalize_skill_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return ()
    names: list[str] = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def routing_category_of(source: Any) -> PdfRoutingCategory:
    if isinstance(source, dict):
        return PdfRoutingCategory.normalize(source.get("routing_category"))
    return PdfRoutingCategory.normalize(getattr(source, "routing_category", None))


def resolve_active_fine_skill_names(
    *,
    profile: Any | None = None,
    manifest: dict[str, Any] | None = None,
    config: Any | None = None,
) -> tuple[str, ...]:
    """Resolve skill names for Full / Progressive / Debug from the frozen owner."""
    if manifest is not None and "fine_skills" in manifest:
        return _normalize_skill_names(manifest.get("fine_skills"))

    names = list(_normalize_skill_names(getattr(config, "fine_skills", ())))
    routing = routing_category_of(profile if profile is not None else manifest)
    if (
        routing is PdfRoutingCategory.ATLAS
        and DRAWING_SHEET_SKILL_NAME not in names
    ):
        names.append(DRAWING_SHEET_SKILL_NAME)
    return tuple(names)


def resolve_fine_skills(names: Sequence[str] | None) -> tuple[FineSkill, ...]:
    return tuple(
        _REGISTERED_FINE_SKILLS[name]
        for name in _normalize_skill_names(names)
        if name in _REGISTERED_FINE_SKILLS
    )


def joined_skill_addon(skills: Sequence[FineSkill], method_name: str) -> str:
    parts: list[str] = []
    for skill in skills:
        text = str(getattr(skill, method_name)() or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def extra_tag_field_keys(skills: Sequence[FineSkill]) -> tuple[str, ...]:
    keys: list[str] = []
    for skill in skills:
        for key in skill.extra_tag_fields:
            if key not in keys:
                keys.append(key)
    return tuple(keys)
