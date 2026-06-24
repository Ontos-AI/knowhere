"""Page-memory asset extraction for figures and tables."""

from __future__ import annotations

import base64
import importlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pandas as pd
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from app.services.document_agent.budget import BudgetTracker
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.page_memory.page_renderer import PageRenderResult
from shared.services.ai.prompt_service import build_prompt
from shared.utils.token_estimate import estimate_tokens

_BUDGET_STAGE = "page_asset_extraction"
_GRID_SIZE = 1000
_VALID_KINDS = {"table", "figure"}
_DEFAULT_ASSET_MODEL = "qwen3.6-flash"
_ASSET_ANNOTATE_DIR = "asset_annotate"


@dataclass
class PageAsset:
    asset_id: str
    page_index: int
    asset_index: int
    kind: str
    bbox_px: list[int]
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float
    caption: str = ""
    confidence: float = 0.0
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    image_uri: str = ""
    html_uri: str = ""
    image_path: str = ""
    html_path: str = ""
    extraction_status: str = "pending"


def page_asset_extraction_enabled() -> bool:
    return os.environ.get(
        "PAGE_MEMORY_ASSET_EXTRACTION_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def get_asset_confidence_threshold() -> float:
    try:
        return float(os.environ.get("PAGE_MEMORY_ASSET_CONFIDENCE_THRESHOLD", "0.3"))
    except ValueError:
        return 0.3


def get_asset_model() -> str | None:
    model = os.environ.get("PAGE_MEMORY_ASSET_MODEL", "").strip()
    if model:
        return model
    return _DEFAULT_ASSET_MODEL


def get_asset_max_pages(page_count: int) -> int:
    try:
        value = int(os.environ.get("PAGE_MEMORY_ASSET_MAX_PAGES", str(page_count)))
    except ValueError:
        value = page_count
    return max(0, min(page_count, value))


def get_asset_budget(page_count: int) -> int:
    default_budget = str(page_count * 600)
    try:
        return max(0, int(os.environ.get("PAGE_MEMORY_ASSET_BUDGET", default_budget)))
    except ValueError:
        return max(0, page_count * 600)


def detect_page_assets(
    *,
    page: PageRenderResult,
    source_name: str,
    model_name: str | None,
    budget: BudgetTracker | None,
    confidence_threshold: float,
) -> list[PageAsset]:
    """Detect asset regions on one rendered page via VLM."""
    if not model_name or not page.image_path or not os.path.exists(page.image_path):
        return []

    try:
        with Image.open(page.image_path) as image:
            width_px, height_px = image.size
        with open(page.image_path, "rb") as handle:
            image_b64 = base64.b64encode(handle.read()).decode()
    except Exception as exc:
        logger.warning(
            "[page_assets] failed to read rendered page {}: {}",
            page.page_index,
            exc,
        )
        return []

    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-asset-detect",
        "",
        "",
        paras={"max_tokens": 1200, "grid_size": _GRID_SIZE},
    )
    est = estimate_tokens(prompt) + 1000
    if budget is not None and not budget.try_reserve(
        "visual", est, stage=_BUDGET_STAGE
    ):
        logger.debug(
            "[page_assets] asset budget exhausted before page {}", page.page_index
        )
        return []

    try:
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        client = get_openai_client(model=model_name)
        raw_response, usage = client.chat_completion_with_usage(
            messages=cast(
                Any,
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}"
                                },
                            },
                        ],
                    }
                ],
            ),
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task="page_memory.asset_detect",
        )
        if budget is not None:
            budget.commit(
                "visual",
                actual=usage.get("total_tokens", est),
                est=est,
                stage=_BUDGET_STAGE,
            )
        data = json.loads(raw_response)
    except Exception as exc:
        logger.warning(
            "[page_assets] VLM asset detection failed on page {}: {}",
            page.page_index,
            exc,
        )
        if budget is not None:
            budget.refund("visual", est=est, stage=_BUDGET_STAGE)
        return []

    regions = data.get("regions") if isinstance(data, dict) else None
    if not isinstance(regions, list):
        return []

    assets: list[PageAsset] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        kind = str(region.get("kind") or region.get("type") or "").strip().lower()
        if kind not in _VALID_KINDS:
            continue
        confidence = _safe_float(region.get("confidence"), default=0.0)
        if confidence < confidence_threshold:
            continue
        bbox_px = _norm_bbox_to_px(region.get("bbox"), width_px, height_px)
        if bbox_px is None:
            continue
        asset_index = len(assets) + 1
        title = str(region.get("title") or region.get("caption") or "").strip()
        asset_id = "asset_" + gen_str_codes(
            f"{source_name}::{page.page_index}::{asset_index}::{kind}::{bbox_px}::"
            f"{title}"
        )
        assets.append(
            PageAsset(
                asset_id=asset_id,
                page_index=page.page_index,
                asset_index=asset_index,
                kind=kind,
                bbox_px=bbox_px,
                width_px=width_px,
                height_px=height_px,
                width_pt=float(page.width or 0.0),
                height_pt=float(page.height or 0.0),
                caption="",
                confidence=confidence,
                title=title,
                summary="",
                keywords=[],
                extraction_status="detected",
            )
        )
    return assets


def crop_page_asset(
    *,
    asset: PageAsset,
    page_image_path: str,
    output_dir: str,
    margin_px: int = 4,
) -> PageAsset:
    image_dir = Path(output_dir) / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    filename = f"image_page_{asset.page_index}_{asset.kind}_{asset.asset_index}.png"
    output_path = image_dir / filename
    try:
        with Image.open(page_image_path) as image:
            x1, y1, x2, y2 = asset.bbox_px
            box = (
                max(0, x1 - margin_px),
                max(0, y1 - margin_px),
                min(image.width, x2 + margin_px),
                min(image.height, y2 + margin_px),
            )
            image.crop(box).save(output_path)
        asset.image_path = str(output_path)
        asset.image_uri = f"images/{filename}"
        if asset.extraction_status == "detected":
            asset.extraction_status = "cropped"
    except Exception as exc:
        logger.warning(
            "[page_assets] crop failed for page {} asset {}: {}",
            asset.page_index,
            asset.asset_index,
            exc,
        )
        asset.extraction_status = "crop_failed"
    return asset


def extract_table_html(
    *,
    asset: PageAsset,
    pdf_path: str,
    output_dir: str,
) -> PageAsset:
    """Extract table HTML with tabula stream mode; degrade on missing Java/package."""
    if asset.kind != "table":
        return asset
    if os.environ.get("PAGE_MEMORY_TABLE_ENGINE", "tabula").strip().lower() != "tabula":
        asset.extraction_status = f"{asset.extraction_status}:table_engine_disabled"
        return asset
    if not _has_working_java():
        asset.extraction_status = f"{asset.extraction_status}:java_unavailable"
        return asset

    try:
        tabula = cast(Any, importlib.import_module("tabula"))
    except Exception as exc:
        logger.warning("[page_assets] tabula-py unavailable: {}", exc)
        asset.extraction_status = f"{asset.extraction_status}:tabula_unavailable"
        return asset

    area_pt = _bbox_px_to_area_pt(
        asset.bbox_px,
        width_px=asset.width_px,
        height_px=asset.height_px,
        width_pt=asset.width_pt,
        height_pt=asset.height_pt,
    )
    if area_pt is None:
        asset.extraction_status = f"{asset.extraction_status}:missing_page_dimensions"
        return asset

    try:
        frames = cast(
            list[Any],
            tabula.read_pdf(
                pdf_path,
                pages=asset.page_index,
                area=area_pt,
                guess=False,
                lattice=False,
                stream=True,
                multiple_tables=True,
                pandas_options={"header": None},
            ),
        )
    except Exception as exc:
        logger.warning(
            "[page_assets] tabula extraction failed page {} asset {}: {}",
            asset.page_index,
            asset.asset_index,
            exc,
        )
        asset.extraction_status = f"{asset.extraction_status}:tabula_failed"
        return asset

    frame = next(
        (
            item
            for item in frames
            if isinstance(item, pd.DataFrame) and _is_meaningful_frame(item)
        ),
        None,
    )
    if frame is None:
        asset.extraction_status = f"{asset.extraction_status}:tabula_empty"
        return asset

    table_dir = Path(output_dir) / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    filename = f"table_page_{asset.page_index}_{asset.asset_index}.html"
    output_path = table_dir / filename
    html = frame.fillna("").astype(str).to_html(index=False, header=False)
    output_path.write_text(html, encoding="utf-8")
    asset.html_path = str(output_path)
    asset.html_uri = f"tables/{filename}"
    asset.extraction_status = "table_html_extracted"
    return asset


def extract_page_assets_from_renders(
    *,
    pdf_path: str,
    rendered_pages: list[PageRenderResult],
    output_dir: str,
    model_name: str | None,
    budget: BudgetTracker | None,
    max_pages: int,
    confidence_threshold: float,
) -> dict[int, list[PageAsset]]:
    pages = sorted(rendered_pages, key=lambda item: item.page_index)[:max_pages]
    source_name = Path(pdf_path).name
    assets_by_page: dict[int, list[PageAsset]] = {}
    for page in pages:
        detected = detect_page_assets(
            page=page,
            source_name=source_name,
            model_name=model_name,
            budget=budget,
            confidence_threshold=confidence_threshold,
        )
        page_assets: list[PageAsset] = []
        for asset in detected:
            crop_page_asset(
                asset=asset,
                page_image_path=page.image_path,
                output_dir=output_dir,
            )
            if asset.kind == "table":
                extract_table_html(
                    asset=asset,
                    pdf_path=pdf_path,
                    output_dir=output_dir,
                )
            if asset.image_uri or asset.html_uri:
                page_assets.append(asset)
        if page_assets:
            assets_by_page[page.page_index] = page_assets
            annotate_page_assets(
                page=page,
                assets=page_assets,
                output_dir=output_dir,
            )
    logger.info(
        "[page_assets] extracted {} assets across {} pages",
        sum(len(items) for items in assets_by_page.values()),
        len(assets_by_page),
    )
    return assets_by_page


def annotate_page_assets(
    *,
    page: PageRenderResult,
    assets: list[PageAsset],
    output_dir: str,
) -> str:
    if not assets or not page.image_path or not os.path.exists(page.image_path):
        return ""
    annotate_dir = Path(output_dir) / _ASSET_ANNOTATE_DIR
    annotate_dir.mkdir(parents=True, exist_ok=True)
    output_path = annotate_dir / f"page_{page.page_index}.png"
    try:
        with Image.open(page.image_path) as image:
            canvas = image.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype(
                "/System/Library/Fonts/Supplemental/Arial.ttf", 22
            )
        except Exception:
            font = ImageFont.load_default()
        for asset in assets:
            x1, y1, x2, y2 = asset.bbox_px
            color = _asset_box_color(asset.kind)
            draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
            label = f"{asset.kind} {asset.confidence:.2f}"
            if asset.title:
                label += f" | {asset.title[:24]}"
            draw.text((x1 + 2, max(0, y1 - 24)), label, fill=color, font=font)
        canvas.save(output_path)
    except Exception as exc:
        logger.warning(
            "[page_assets] failed to write asset annotation for page {}: {}",
            page.page_index,
            exc,
        )
        return ""
    return str(output_path)


def _asset_box_color(kind: str) -> tuple[int, int, int]:
    if kind == "table":
        return (220, 0, 0)
    return (0, 150, 0)


def build_asset_rows(
    page_assets_by_page: dict[int, list[PageAsset]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page_index in sorted(page_assets_by_page):
        for asset in page_assets_by_page[page_index]:
            ref_uri = (
                asset.html_uri
                if asset.kind == "table" and asset.html_uri
                else asset.image_uri
            )
            if not ref_uri:
                continue
            row_type = "table" if asset.kind == "table" and asset.html_uri else "image"
            content = _asset_content(asset, row_type=row_type)
            summary = "\n".join(
                part for part in [asset.title.strip(), asset.summary.strip()] if part
            )
            rows.append(
                {
                    "content": content,
                    "path": ref_uri,
                    "type": row_type,
                    "length": len(content),
                    "keywords": ";".join(asset.keywords),
                    "summary": summary,
                    "know_id": asset.asset_id,
                    "tokens": "",
                    "connectto": "",
                    "addtime": get_str_time(),
                    "page_nums": str(asset.page_index),
                    "extra_metadata": {
                        "page_index": asset.page_index,
                        "asset_index": asset.asset_index,
                        "asset_kind": asset.kind,
                        "bbox_px": asset.bbox_px,
                        "confidence": asset.confidence,
                        "title": asset.title,
                        "caption": asset.caption,
                        "image_uri": asset.image_uri,
                        "html_uri": asset.html_uri,
                        "extraction_status": asset.extraction_status,
                        "table_engine": "tabula" if asset.kind == "table" else "",
                    },
                }
            )
    return rows


def asset_reference(asset: PageAsset) -> str:
    uri = (
        asset.html_uri if asset.kind == "table" and asset.html_uri else asset.image_uri
    )
    return f"[{uri}]" if uri else ""


def _asset_content(asset: PageAsset, *, row_type: str) -> str:
    if row_type == "table" and asset.html_uri:
        return asset.html_uri
    parts = [asset.title, asset.summary, asset.caption]
    if asset.image_uri:
        parts.append(f"[{asset.image_uri}]")
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _norm_bbox_to_px(value: object, width_px: int, height_px: int) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    x1_px = round(max(0.0, min(_GRID_SIZE, x1)) / _GRID_SIZE * width_px)
    x2_px = round(max(0.0, min(_GRID_SIZE, x2)) / _GRID_SIZE * width_px)
    y1_px = round(max(0.0, min(_GRID_SIZE, y1)) / _GRID_SIZE * height_px)
    y2_px = round(max(0.0, min(_GRID_SIZE, y2)) / _GRID_SIZE * height_px)
    if x2_px <= x1_px or y2_px <= y1_px:
        return None
    return [x1_px, y1_px, x2_px, y2_px]


def _bbox_px_to_area_pt(
    bbox_px: list[int],
    *,
    width_px: int,
    height_px: int,
    width_pt: float,
    height_pt: float,
    margin_pt: float = 2.0,
) -> list[float] | None:
    if width_px <= 0 or height_px <= 0 or width_pt <= 0 or height_pt <= 0:
        return None
    x1, y1, x2, y2 = bbox_px
    left = x1 / width_px * width_pt
    right = x2 / width_px * width_pt
    top = y1 / height_px * height_pt
    bottom = y2 / height_px * height_pt
    return [
        round(max(0.0, top - margin_pt), 2),
        round(max(0.0, left - margin_pt), 2),
        round(min(height_pt, bottom + margin_pt), 2),
        round(min(width_pt, right + margin_pt), 2),
    ]


def _has_working_java() -> bool:
    java_path = _resolve_working_java()
    if java_path is None:
        return False
    java_bin = str(java_path.parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if java_bin not in path_parts:
        os.environ["PATH"] = os.pathsep.join([java_bin, *path_parts])
    os.environ.setdefault("JAVA_HOME", str(java_path.parent.parent))
    return True


def _resolve_working_java() -> Path | None:
    candidates: list[Path] = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / "java")

    which_java = shutil.which("java")
    if which_java:
        candidates.append(Path(which_java))

    candidates.extend(
        [
            Path("/opt/homebrew/opt/java/bin/java"),
            Path("/usr/local/opt/java/bin/java"),
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists():
            continue
        if _java_version_ok(candidate):
            return candidate
    return None


def _java_version_ok(java_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(java_path), "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    combined = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 0 and "Unable to locate a Java Runtime" not in combined


def _is_meaningful_frame(frame: pd.DataFrame) -> bool:
    if frame.empty or frame.shape[1] == 0:
        return False
    non_empty = frame.fillna("").astype(str).map(lambda text: bool(text.strip()))
    return bool(non_empty.values.sum() >= 2)


def _normalize_keywords(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        delimiter = ";" if ";" in value else ","
        return [item.strip() for item in value.split(delimiter) if item.strip()]
    return []


def _safe_float(value: object, *, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "PageAsset",
    "annotate_page_assets",
    "asset_reference",
    "build_asset_rows",
    "extract_page_assets_from_renders",
    "get_asset_budget",
    "get_asset_confidence_threshold",
    "get_asset_max_pages",
    "get_asset_model",
    "page_asset_extraction_enabled",
]
