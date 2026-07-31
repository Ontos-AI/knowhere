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

from app.services.document_agent.visual import visual_debug_enabled
from app.services.document_parser.support.identifiers import gen_str_codes
from app.services.page_memory.page_renderer import PageRenderResult
from shared.services.ai.prompt_service import build_prompt
from shared.services.chunks.canonical_chunk_builder import ChunkMetadata, ChunkPayload

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
    confidence: float = 0.0
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    entities: list[dict[str, str]] = field(default_factory=list)
    image_uri: str = ""
    html_uri: str = ""
    image_path: str = ""
    html_path: str = ""
    extraction_status: str = "pending"
    source_page_nums: list[int] = field(default_factory=list)
    """Physical pages this asset spans (defaults to ``[page_index]``)."""

    def __post_init__(self) -> None:
        self.source_page_nums = normalize_asset_source_pages(
            self.source_page_nums,
            fallback_page=self.page_index,
        )


def normalize_asset_source_pages(
    pages: list[int] | None,
    *,
    fallback_page: int,
) -> list[int]:
    """Return sorted unique positive page numbers, falling back to one page."""
    normalized: set[int] = set()
    for page in pages or []:
        try:
            value = int(page)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized.add(value)
    if normalized:
        return sorted(normalized)
    try:
        fallback = int(fallback_page)
    except (TypeError, ValueError):
        return []
    return [fallback] if fallback > 0 else []


def asset_source_pages(asset: PageAsset) -> list[int]:
    """Pages covered by an asset, including cross-page merged tables."""
    return normalize_asset_source_pages(
        asset.source_page_nums,
        fallback_page=asset.page_index,
    )


def page_asset_extraction_enabled() -> bool:
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    return PageMemoryConfig.default().asset_extraction_enabled


def page_asset_summary_enabled() -> bool:
    """Whether detected page assets are summarized via the engine (§4.3).

    Gated separately from detection so the richer chart/figure summarization can
    be rolled out independently. Defaults on via ``PageMemoryConfig``.
    """
    from shared.models.schemas.page_memory_config import PageMemoryConfig

    return PageMemoryConfig.default().asset_summary_enabled


def get_asset_confidence_threshold() -> float:
    return 0.3


def get_asset_model() -> str | None:
    return _DEFAULT_ASSET_MODEL


def get_asset_max_pages(page_count: int) -> int:
    return max(0, page_count)


def detect_page_assets(
    *,
    page: PageRenderResult,
    source_name: str,
    model_name: str | None,
    budget: Any | None = None,
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

    try:
        from shared.services.ai.llm_overrides import get_vision_client

        client, model_name = get_vision_client(requested_model=model_name)
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
        data = json.loads(raw_response)
    except Exception as exc:
        logger.warning(
            "[page_assets] VLM asset detection failed on page {}: {}",
            page.page_index,
            exc,
        )
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
        title = str(region.get("title") or "").strip()
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
    table_engine: str = "tabula",
) -> PageAsset:
    """Extract table HTML with tabula stream mode; degrade on missing Java/package."""
    if asset.kind != "table":
        return asset
    if table_engine.strip().lower() != "tabula":
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


def summarize_page_asset(
    *,
    asset: PageAsset,
    model_name: str | None,
    budget: Any | None = None,
) -> PageAsset:
    """Summarize a cropped asset via the unified engine (§4.3).

    Routes tables (with extracted HTML) through the text asset path and figures /
    charts through the image asset path. Populates ``summary`` and a ``title``
    when the asset had none. Assets do not extract entities.
    """
    from shared.services.ai.summary.engine import summarize

    table_html = ""
    if asset.kind == "table" and asset.html_path and os.path.exists(asset.html_path):
        try:
            table_html = Path(asset.html_path).read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("[page_assets] failed to read table html: {}", exc)

    if table_html:
        result = summarize(
            mode="asset",
            text=table_html,
            model=model_name,
            usage_task="page_memory.asset_summary_text",
            asset_title_hint=asset.title,
        )
    elif asset.image_path and os.path.exists(asset.image_path):
        result = summarize(
            mode="asset",
            image_paths=[asset.image_path],
            text=asset.title,
            model=model_name,
            usage_task="page_memory.asset_summary_visual",
            asset_title_hint=asset.title,
        )
    else:
        return asset

    if result.title and not asset.title:
        asset.title = result.title
    if result.summary:
        asset.summary = result.summary
        asset.extraction_status = "summarized"
    asset.entities = []
    asset.keywords = []
    return asset


def extract_page_assets_from_renders(
    *,
    pdf_path: str,
    rendered_pages: list[PageRenderResult],
    output_dir: str,
    model_name: str | None,
    budget: Any | None = None,
    max_pages: int,
    confidence_threshold: float,
    summary_enabled: bool = True,
    summary_concurrency: int = 4,
    table_engine: str = "tabula",
    table_merge_enabled: bool = True,
) -> dict[int, list[PageAsset]]:
    pages = sorted(rendered_pages, key=lambda item: item.page_index)[:max_pages]
    source_name = Path(pdf_path).name
    assets_by_page: dict[int, list[PageAsset]] = {}
    pending_summaries: list[PageAsset] = []

    for page in pages:
        detected = detect_page_assets(
            page=page,
            source_name=source_name,
            model_name=model_name,
            confidence_threshold=confidence_threshold,
        )
        page_assets_list: list[PageAsset] = []
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
                    table_engine=table_engine,
                )
            if asset.image_uri or asset.html_uri:
                page_assets_list.append(asset)
                if summary_enabled:
                    pending_summaries.append(asset)
        if page_assets_list:
            assets_by_page[page.page_index] = page_assets_list
            if visual_debug_enabled():
                annotate_page_assets(
                    page=page,
                    assets=page_assets_list,
                    output_dir=output_dir,
                )

    if table_merge_enabled:
        assets_by_page = merge_cross_page_tables(
            assets_by_page=assets_by_page,
            output_dir=output_dir,
            model_name=model_name,
        )

    if pending_summaries:
        pending_summaries = [a for a in pending_summaries if a.html_path or a.image_path]
        _batch_summarize_assets(
            assets=pending_summaries,
            model_name=model_name,
            max_concurrent=summary_concurrency,
        )

    logger.info(
        "[page_assets] extracted {} assets across {} pages",
        sum(len(items) for items in assets_by_page.values()),
        len(assets_by_page),
    )
    return assets_by_page


def _batch_summarize_assets(
    *,
    assets: list[PageAsset],
    model_name: str | None,
    max_concurrent: int,
) -> None:
    """Summarize assets in parallel using a gevent pool."""
    import gevent
    from gevent.pool import Pool as GeventPool

    logger.info(
        "[page_assets] batch-summarizing {} assets (concurrency={})",
        len(assets),
        max_concurrent,
    )
    pool = GeventPool(size=min(max_concurrent, len(assets)))
    greenlets = [
        pool.spawn(summarize_page_asset, asset=asset, model_name=model_name)
        for asset in assets
    ]
    gevent.joinall(greenlets)


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


def build_asset_chunks(
    page_assets_by_page: dict[int, list[PageAsset]],
) -> list[ChunkPayload]:
    """Build canonical asset chunks directly from extracted page assets."""
    chunks: list[ChunkPayload] = []
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
            metadata: ChunkMetadata = {
                "length": len(content),
                "summary": asset.summary.strip(),
                "keywords": list(asset.keywords),
                "entities": list(asset.entities),
                "tokens": [],
                "connect_to": [],
                "page_nums": asset_source_pages(asset),
                "file_path": ref_uri,
                "original_name": os.path.basename(ref_uri),
            }
            if asset.title.strip():
                metadata["asset_title"] = asset.title.strip()
            metadata.update(_asset_extra_metadata(asset))
            chunks.append(
                {
                    "chunk_id": asset.asset_id,
                    "type": row_type,
                    "content": content,
                    "path": ref_uri,
                    "metadata": metadata,
                    "order": len(chunks),
                }
            )
    return chunks


def _asset_extra_metadata(asset: PageAsset) -> ChunkMetadata:
    metadata: ChunkMetadata = {
        "image_uri": asset.image_uri,
    }
    return metadata


def asset_reference(asset: PageAsset) -> str:
    uri = (
        asset.html_uri if asset.kind == "table" and asset.html_uri else asset.image_uri
    )
    return f"[{uri}]" if uri else ""


def _asset_content(asset: PageAsset, *, row_type: str) -> str:
    if row_type == "table" and asset.html_uri:
        return asset.html_uri
    parts = [asset.title, asset.summary]
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


# ── C5b: cross-page table merge ──────────────────────────────────────


def merge_cross_page_tables(
    *,
    assets_by_page: dict[int, list[PageAsset]],
    output_dir: str,
    model_name: str | None,
) -> dict[int, list[PageAsset]]:
    """Detect and merge tables that span consecutive pages (C5b)."""
    sorted_pages = sorted(assets_by_page.keys())
    merged_count = 0

    i = 0
    while i < len(sorted_pages) - 1:
        curr_page = sorted_pages[i]
        next_page = sorted_pages[i + 1]

        if next_page != curr_page + 1:
            i += 1
            continue

        curr_tables = [
            a for a in assets_by_page.get(curr_page, [])
            if a.kind == "table" and a.html_path and os.path.exists(a.html_path)
        ]
        next_tables = [
            a for a in assets_by_page.get(next_page, [])
            if a.kind == "table" and a.html_path and os.path.exists(a.html_path)
        ]
        if not curr_tables or not next_tables:
            i += 1
            continue

        tail_table = curr_tables[-1]
        head_table = next_tables[0]

        tail_cols = _count_html_columns(tail_table.html_path)
        head_cols = _count_html_columns(head_table.html_path)
        if tail_cols != head_cols or tail_cols == 0:
            i += 1
            continue

        is_continuation, header_rows_to_skip = _llm_judge_table_continuity(
            tail_html_path=tail_table.html_path,
            head_html_path=head_table.html_path,
            col_count=tail_cols,
            model_name=model_name,
        )
        if not is_continuation:
            logger.debug(
                "[page_assets] table merge skip: page {} → {} (LLM: not continuation)",
                curr_page, next_page,
            )
            i += 1
            continue

        _merge_table_html_files(
            tail_asset=tail_table,
            head_asset=head_table,
            header_rows_to_skip=header_rows_to_skip,
        )
        tail_table.source_page_nums = normalize_asset_source_pages(
            [*asset_source_pages(tail_table), *asset_source_pages(head_table)],
            fallback_page=tail_table.page_index,
        )
        assets_by_page[next_page].remove(head_table)
        if not assets_by_page[next_page]:
            del assets_by_page[next_page]
            sorted_pages.remove(next_page)
        merged_count += 1
        logger.info(
            "[page_assets] merged cross-page table: page {} + {} (header_skip={})",
            curr_page, next_page, header_rows_to_skip,
        )
        # Do not advance i — check if the merged table continues to the next page

    if merged_count:
        logger.info("[page_assets] C5b merged {} cross-page table(s)", merged_count)
    return assets_by_page


def _count_html_columns(html_path: str) -> int:
    """Count columns by parsing the first <tr> in an HTML table file."""
    try:
        from bs4 import BeautifulSoup, Tag

        html = Path(html_path).read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        first_row = soup.find("tr")
        if not isinstance(first_row, Tag):
            return 0
        return len(first_row.find_all(["td", "th"]))
    except Exception:
        return 0


def _extract_boundary_rows(html_path: str, position: str, max_rows: int = 3) -> str:
    """Extract first or last N <tr> elements as raw HTML string."""
    try:
        from bs4 import BeautifulSoup

        html = Path(html_path).read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        all_rows = soup.find_all("tr")
        if not all_rows:
            return ""
        if position == "tail":
            selected = all_rows[-max_rows:]
        else:
            selected = all_rows[:max_rows]
        return "\n".join(str(row) for row in selected)
    except Exception:
        return ""


def _llm_judge_table_continuity(
    *,
    tail_html_path: str,
    head_html_path: str,
    col_count: int,
    model_name: str | None,
) -> tuple[bool, int]:
    """Ask LLM whether two adjacent tables are a cross-page continuation.

    Returns (is_continuation, header_rows_to_skip).
    """
    header_rows = _extract_boundary_rows(tail_html_path, "head", max_rows=3)
    tail_rows = _extract_boundary_rows(tail_html_path, "tail", max_rows=3)
    head_rows = _extract_boundary_rows(head_html_path, "head", max_rows=5)
    if not tail_rows or not head_rows:
        return False, 0

    prompt, temperature, _top_p, max_tokens = build_prompt(
        "page-memory-table-continuity",
        "",
        "",
        paras={
            "tail_rows": tail_rows,
            "head_rows": head_rows,
            "header_rows": header_rows,
        },
    )

    try:
        from shared.services.ai.llm_overrides import get_text_client

        client, model_name = get_text_client(requested_model=model_name)
        raw_response, usage = client.chat_completion_with_usage(
            messages=[{"role": "user", "content": prompt}],
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            usage_task="page_memory.table_continuity",
        )
        data = json.loads(raw_response)
        is_continuation = bool(data.get("is_continuation", False))
        header_rows_to_skip = max(0, int(data.get("header_rows_to_skip", 0)))
        return is_continuation, header_rows_to_skip
    except Exception as exc:
        logger.warning("[page_assets] table continuity LLM call failed: {}", exc)
        return False, 0


def _merge_table_html_files(
    *,
    tail_asset: PageAsset,
    head_asset: PageAsset,
    header_rows_to_skip: int,
) -> None:
    """Append head table rows into tail table HTML file and clean up head."""
    from bs4 import BeautifulSoup

    tail_html = Path(tail_asset.html_path).read_text(encoding="utf-8")
    head_html = Path(head_asset.html_path).read_text(encoding="utf-8")

    tail_soup = BeautifulSoup(tail_html, "html.parser")
    head_soup = BeautifulSoup(head_html, "html.parser")

    from bs4 import Tag

    _tail_container = tail_soup.find("tbody") or tail_soup.find("table")
    _head_container = head_soup.find("tbody") or head_soup.find("table")
    if not isinstance(_tail_container, Tag) or not isinstance(_head_container, Tag):
        return

    tail_container: Tag = _tail_container
    head_rows = _head_container.find_all("tr")

    rows_to_append = head_rows[header_rows_to_skip:]
    for row in rows_to_append:
        tail_container.append(row)

    Path(tail_asset.html_path).write_text(str(tail_soup), encoding="utf-8")

    try:
        Path(head_asset.html_path).unlink(missing_ok=True)
    except Exception as exc:
        logger.debug(
            "[page_assets] failed to remove merged table head html {}: {}",
            head_asset.html_path,
            exc,
        )
    if head_asset.image_path:
        try:
            Path(head_asset.image_path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(
                "[page_assets] failed to remove merged table head image {}: {}",
                head_asset.image_path,
                exc,
            )


__all__ = [
    "PageAsset",
    "annotate_page_assets",
    "asset_reference",
    "asset_source_pages",
    "build_asset_chunks",
    "extract_page_assets_from_renders",
    "get_asset_confidence_threshold",
    "get_asset_max_pages",
    "get_asset_model",
    "merge_cross_page_tables",
    "normalize_asset_source_pages",
    "page_asset_extraction_enabled",
    "page_asset_summary_enabled",
    "summarize_page_asset",
]
