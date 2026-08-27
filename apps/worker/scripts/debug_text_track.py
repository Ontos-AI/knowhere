#!/usr/bin/env python3
# ruff: noqa: E402
"""Staged text-track document parsing debug script.

Supports PDF (shard-aware), DOCX, and MD formats with four breakpoints:
  1. profile        — production-aligned PROFILE:
                      run_coarse → lightweight (≤MAX) / structural (>MAX)
  2. mineru         — shard splitting + MinerU extraction
  3. hierarchy      — heading prediction → merged hierarchy tree
  4. full           — complete extraction → chunks/doc_nav/manifest

Output directory:
  ~/.knowhere/_debug_parse/<docname>/text_track/

Usage:
  cd apps/worker
  uv run python scripts/debug_text_track.py --file /path/to/doc.pdf
  uv run python scripts/debug_text_track.py --file /path/to/doc.pdf --stop-at profile
  uv run python scripts/debug_text_track.py --sjsyj --stop-at mineru --reuse-profile
  uv run python scripts/debug_text_track.py --sjsyj --stop-at hierarchy --reuse-mineru
  uv run python scripts/debug_text_track.py --file /path/to/doc.docx --stop-at hierarchy
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

# ── Bootstrap ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[3]
WORKER_ROOT = ROOT / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "packages" / "shared-python"))

from dotenv import load_dotenv

load_dotenv(WORKER_ROOT / ".env")
os.environ.setdefault("LOCAL_DEBUG", "1")
os.environ.setdefault("OVERSIZED_PDF_SHARD_ENABLED", "true")

from loguru import logger

from shared.services.ai.token_tracking import (
    cleanup_token_tracker,
    get_current_token_tracker,
    init_token_tracker,
)

from _debug_token_ledger import (
    aggregate_stage_deltas,
    empty_token_usage,
    load_stage_ledger,
    record_stage_delta,
    token_usage_delta,
)

# ── Constants ───────────────────────────────────────────────────────────────
DEFAULT_SJSYJ_PDF = Path(
    "/Users/wuchengke/Desktop/temp/test_docs/"
    "SJSYJ-SC-2024 企业制度汇编（上册）.pdf"
)
DEFAULT_SPACEX_PDF = Path("/Users/wuchengke/Desktop/temp/test_docs/spacex-s1.pdf")
OUTPUT_ROOT = Path("~/.knowhere/_debug_parse").expanduser()
TOKEN_LEDGER_NAME = "token_ledger.json"
TOKEN_LEDGER_STAGES = ("profile", "mineru", "hierarchy", "full")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("   → {}", path)


def _load_token_ledger(out_dir: Path) -> dict[str, Any]:
    return load_stage_ledger(out_dir / TOKEN_LEDGER_NAME)


def _record_token_stage(
    ledger: dict[str, Any],
    stage: str,
    *,
    prev: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    current = deepcopy(get_current_token_tracker() or {})
    record_stage_delta(
        ledger,
        stage=stage,
        stage_keys=TOKEN_LEDGER_STAGES,
        prev=prev,
        current=current,
        out_path=out_dir / TOKEN_LEDGER_NAME,
    )
    logger.info("   → {}", out_dir / TOKEN_LEDGER_NAME)
    return current


def _aggregate_token_ledger(
    ledger: dict[str, Any],
    *,
    remainder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return aggregate_stage_deltas(
        ledger,
        TOKEN_LEDGER_STAGES,
        remainder=remainder,
    )


def _apply_token_usage_to_outputs(
    out_dir: Path,
    trace: dict[str, Any],
    usage: dict[str, Any],
) -> None:
    trace["token_usage"] = usage
    _write_json(out_dir / "trace.json", trace)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        return
    processing = manifest.setdefault("processing", {})
    if isinstance(processing, dict):
        processing["token_usage"] = usage
        _write_json(manifest_path, manifest)


# ── Stage 1: Profile + Shard Plan (PDF only) ───────────────────────────────

def _stage_profile(pdf_path: str, filename: str, out_dir: Path, model: str | None):
    """Production-aligned profile: coarse → lightweight / structural."""
    from app.services.document_agent.coordinator import ProfileCoordinator
    from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
    from shared.core.config import settings

    logger.info("=" * 70)
    logger.info("🧬 Stage 1: PROFILE (production-aligned)")
    logger.info("=" * 70)

    doc_agent_dir = out_dir / "_doc_agent"
    doc_agent_dir.mkdir(parents=True, exist_ok=True)

    vlm_model = model or settings.IMAGE_MODEL
    coordinator = ProfileCoordinator(
        pdf_path=pdf_path,
        job_id=filename,
        output_dir=str(doc_agent_dir),
        model=vlm_model,
        settings={
            "vlm_model": vlm_model,
            "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
            # Debug PROFILE must exercise the same TOC → run_toc_anchoring path
            # as production (PDF_PROFILE_TOC_ENABLED defaults True; keep explicit
            # so a local kill-switch .env cannot silently skip TOC).
            "toc_profile_enabled": True,
        },
    )
    t0 = time.time()
    agent_profile = coordinator.run_coarse()
    page_count = int(coordinator.blackboard.page_count or 0)
    routing = PdfRoutingCategory.normalize(agent_profile.routing_category)
    if routing is PdfRoutingCategory.ATLAS:
        raise RuntimeError(
            "Atlas routing skips text-track anatomy (same as production). "
            "Use page-memory / atlas debug paths for this document."
        )
    if page_count > settings.MAX_PDF_PAGE_LIMIT:
        logger.info(
            "   page_count={} > MAX_PDF_PAGE_LIMIT={} → run_structural",
            page_count,
            settings.MAX_PDF_PAGE_LIMIT,
        )
        anatomy = coordinator.run_structural()
    else:
        logger.info(
            "   page_count={} ≤ MAX_PDF_PAGE_LIMIT={} → run_lightweight_anatomy",
            page_count,
            settings.MAX_PDF_PAGE_LIMIT,
        )
        anatomy = coordinator.run_lightweight_anatomy()
    elapsed = time.time() - t0

    profile = coordinator.blackboard.document_profile or agent_profile
    margins = coordinator.blackboard.global_signals.get("content_margins") or {}
    header_y = getattr(profile, "header_y", None)
    footer_y = getattr(profile, "footer_y", None)
    if header_y is None:
        header_y = margins.get("header_y")
    if footer_y is None:
        footer_y = margins.get("footer_y")

    logger.info("   profile done in {:.1f}s", elapsed)
    logger.info(
        "   category={!r} routing={} header_y={} footer_y={}",
        getattr(profile, "category", None),
        getattr(profile, "routing_category", None),
        header_y,
        footer_y,
    )
    logger.info("   page_count={}", anatomy.page_count)
    logger.info("   toc_pages={}", anatomy.toc_result.toc_pages)
    logger.info("   shard_plan.enabled={}", anatomy.shard_plan.enabled)
    logger.info("   shard_count={}", len(anatomy.shard_plan.shards))
    for shard in anatomy.shard_plan.shards:
        logger.info(
            "     shard_{}: p{}-{} ({} pages, anchor={})",
            shard.shard_index, shard.page_start, shard.page_end,
            shard.page_end - shard.page_start + 1, shard.anchor_type,
        )
    pending = list(getattr(anatomy, "pending_skeleton_anchors", None) or [])
    if pending:
        for record in pending:
            toc = record.get("toc") if isinstance(record, dict) else None
            logger.info(
                "   pending toc_range={} relationship={} grafted={}",
                (toc or {}).get("toc_range") if isinstance(toc, dict) else None,
                record.get("relationship"),
                bool(record.get("grafted")),
            )
    else:
        logger.info("   pending TOC: none")

    return anatomy, elapsed, {
        "profile": profile.to_dict() if profile is not None else None,
        "path": (
            "structural"
            if page_count > settings.MAX_PDF_PAGE_LIMIT
            else "lightweight"
        ),
        "asset_pages": sum(
            1 for feature in coordinator.blackboard.page_features if feature.has_asset
        ),
        "pending_count": len(pending),
        "pending": [
            {
                "toc_range": (record.get("toc") or {}).get("toc_range")
                if isinstance(record.get("toc"), dict)
                else None,
                "relationship": record.get("relationship"),
                "grafted": bool(record.get("grafted")),
            }
            for record in pending
            if isinstance(record, dict)
        ],
        "skeleton_node_count": len(list(getattr(anatomy, "skeleton_nodes", None) or [])),
    }


def _load_anatomy_cache(out_dir: Path, pdf_path: str, filename: str):
    from app.services.document_agent.manifest import (
        PageAnatomyMap, PageFeature, PageLabel,
        Shard, ShardPlan, TocResult, ValidationReport,
    )

    cache_path = out_dir / "_doc_agent" / "anatomy_map.json"
    if not cache_path.exists():
        # Fallback: check sibling page_memory dir (shared profile output)
        sibling = out_dir.parent / "page_memory" / "_doc_agent" / "anatomy_map.json"
        if sibling.exists():
            cache_path = sibling
        else:
            raise FileNotFoundError(f"No cached anatomy: {cache_path}")

    logger.info("⏩ Reusing cached anatomy: {}", cache_path)
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    toc = data.get("toc_result") or {}
    sp = data.get("shard_plan") or {}

    page_features = [
        PageFeature(
            page=int(pf.get("page", 0)),
            raw_text_length=int(pf.get("raw_text_length", 0)),
            text_density=float(pf.get("text_density", 0)),
            image_coverage=float(pf.get("image_coverage", 0)),
            image_count=int(pf.get("image_count", 0)),
            table_count=int(pf.get("table_count", 0)),
            drawings_count=int(pf.get("drawings_count", 0)),
            orientation=pf.get("orientation", "portrait"),
            width=float(pf.get("width", 0)),
            height=float(pf.get("height", 0)),
            has_asset=bool(pf.get("has_asset", False)),
            is_blank_like=bool(pf.get("is_blank_like", False)),
        )
        for pf in data.get("page_features", [])
    ]
    page_labels = [
        PageLabel(
            page=int(pl.get("page", 0)),
            kind=pl.get("kind", "normal"),
            evidence=pl.get("evidence", {}),
        )
        for pl in data.get("page_labels", [])
    ]

    return PageAnatomyMap(
        job_id=data.get("job_id", filename),
        file_path=data.get("file_path", pdf_path),
        page_count=int(data.get("page_count", 0)),
        page_features=page_features,
        page_labels=page_labels,
        toc_result=TocResult(
            toc_pages=list(toc.get("toc_pages", [])),
            method=toc.get("method", "none"),
        ),
        shard_plan=ShardPlan(
            enabled=bool(sp.get("enabled", False)),
            reason=sp.get("reason", "not_needed"),
            shards=[
                Shard(
                    shard_index=int(s.get("shard_index", i)),
                    page_start=int(s.get("page_start", 1)),
                    page_end=int(s.get("page_end", 1)),
                    page_offset=int(s.get("page_offset", 0)),
                    anchor_type=s.get("anchor_type", "forced_max_size"),
                    anchor_evidence=s.get("anchor_evidence", ""),
                    toc_hierarchies=(
                        list(s["toc_hierarchies"])
                        if isinstance(s.get("toc_hierarchies"), list)
                        else None
                    ),
                )
                for i, s in enumerate(sp.get("shards", []))
            ],
            validation=ValidationReport(valid=True),
        ),
        toc_hierarchies=data.get("toc_hierarchies"),
        skeleton_anchor=data.get("skeleton_anchor")
        if isinstance(data.get("skeleton_anchor"), dict)
        else None,
        skeleton_nodes=list(data.get("skeleton_nodes") or [])
        if isinstance(data.get("skeleton_nodes"), list)
        else None,
        pending_skeleton_anchors=list(data.get("pending_skeleton_anchors") or [])
        if isinstance(data.get("pending_skeleton_anchors"), list)
        else [],
        global_signals=dict(data.get("global_signals") or {})
        if isinstance(data.get("global_signals"), dict)
        else {},
    )


# ── Stage 2: MinerU Extraction (PDF only) ────────────────────────────────────

def _stage_mineru_pdf(
    pdf_path: str,
    filename: str,
    out_dir: Path,
    anatomy,
) -> tuple[list[str], float]:
    """Split PDF into shards and run MinerU extraction only (no heading prediction)."""
    from app.services.document_parser.formats.pdf.shard_splitter import (
        map_agent_shards,
        split_pdf,
    )
    from app.services.document_parser.providers.mineru.pdf_service import parse_via_full

    logger.info("=" * 70)
    logger.info("🔄 Stage 2: Shard splitting + MinerU extraction")
    logger.info("=" * 70)

    t0 = time.time()
    agent_shards = anatomy.shard_plan.shards

    toc_pages: set[int] = set()
    if anatomy.toc_result and anatomy.toc_result.toc_pages:
        toc_pages = set(anatomy.toc_result.toc_pages)

    merged_shards = map_agent_shards(agent_shards)
    logger.info("   {} agent shards → {} MinerU shards", len(agent_shards), len(merged_shards))

    work_dir = str(out_dir / "_shards")
    os.makedirs(work_dir, exist_ok=True)

    fast_path = len(merged_shards) == 1 and not toc_pages

    if fast_path:
        logger.info("   single shard, no TOC exclusion → fast path (no split)")
        shard_out = os.path.join(work_dir, "shard_0")
        os.makedirs(shard_out, exist_ok=True)
        parse_via_full(pdf_path, filename, shard_out)
        shard_output_dirs = [shard_out]
    else:
        shard_pdf_paths, _page_remap = split_pdf(
            pdf_path, merged_shards, work_dir,
            exclude_pages=toc_pages if toc_pages else None,
        )
        logger.info("   split into {} shard PDFs (excluded {} TOC pages)",
                    len(shard_pdf_paths), len(toc_pages))

        shard_output_dirs: list[str] = [None] * len(shard_pdf_paths)  # type: ignore[list-item]
        for i, shard_pdf in enumerate(shard_pdf_paths):
            shard_out = os.path.join(work_dir, f"shard_{i}")
            os.makedirs(shard_out, exist_ok=True)
            shard_filename = f"{os.path.splitext(filename)[0]}_shard{i}.pdf"
            logger.info("   🔄 MinerU shard_{}: parsing...", i)
            parse_via_full(shard_pdf, shard_filename, shard_out)
            shard_output_dirs[i] = shard_out

    elapsed = time.time() - t0
    logger.info("   ✅ MinerU extraction done in {:.1f}s → {} shard dirs", elapsed, len(shard_output_dirs))
    return shard_output_dirs, elapsed


# ── Stage 3: Per-Shard Heading Prediction + Hierarchy Tree ────────────────────

def _stage_hierarchy_pdf(
    out_dir: Path,
    anatomy,
    model: str | None,
) -> tuple[list[str], float]:
    """Run heading prediction on each shard's full.md, merge, and output hierarchy tree."""
    from app.services.document_parser.formats.markdown.parser import (
        eval_md_headings,
        merge_html_tables,
    )
    from app.services.document_parser.formats.pdf.shard_merger import (
        merge_images,
        merge_shard_lines,
    )
    from app.services.document_parser.formats.pdf.shard_splitter import map_agent_shards

    logger.info("=" * 70)
    logger.info("🔬 Stage 3: Per-shard heading prediction → merged hierarchy")
    logger.info("=" * 70)

    t0 = time.time()
    agent_shards = anatomy.shard_plan.shards

    merged_shards = map_agent_shards(agent_shards)

    work_dir = out_dir / "_shards"

    # Discover shard output dirs
    shard_output_dirs: list[str] = []
    for i in range(len(merged_shards)):
        shard_dir = str(work_dir / f"shard_{i}")
        if not os.path.isdir(shard_dir):
            raise FileNotFoundError(f"shard_{i} dir not found: {shard_dir} (run --stop-at mineru first)")
        shard_output_dirs.append(shard_dir)

    hierarchy_model = model or os.environ.get("NORMOL_MODEL")

    def _predict_shard(shard_idx: int, shard_out_dir: str) -> list[str]:
        md_path = os.path.join(shard_out_dir, "full.md")
        if not os.path.exists(md_path):
            raise FileNotFoundError(f"shard_{shard_idx}: full.md not found at {md_path}")

        with open(md_path, "r", encoding="utf-8") as f:
            md_lines = f.readlines()
        md_lines = [line.strip() for line in md_lines if line.strip()]
        md_lines = merge_html_tables(md_lines)

        is_first = shard_idx == 0
        agent_shard = agent_shards[shard_idx]
        shard_toc = getattr(agent_shard, "toc_hierarchies", None)

        lines_with_heading = eval_md_headings(
            md_lines,
            source_type="md",
            toc_hierarchies=shard_toc,
            smart_parse=True,
            model_name=hierarchy_model,
            output_dir=shard_out_dir,
            layout_json_path=(
                os.path.join(shard_out_dir, "layout.json")
                if os.path.exists(os.path.join(shard_out_dir, "layout.json"))
                else None
            ),
            is_first_shard=is_first,
        )

        heading_count = sum(1 for line in lines_with_heading if line.startswith("#"))
        logger.info("   ✅ shard_{}: {} headings from {} lines",
                    shard_idx, heading_count, len(lines_with_heading))

        _write_json(
            Path(shard_out_dir) / "lines_with_heading.json",
            lines_with_heading,
        )
        return lines_with_heading

    all_shard_lines: list[list[str]] = []
    for i, shard_dir in enumerate(shard_output_dirs):
        lines = _predict_shard(i, shard_dir)
        all_shard_lines.append(lines)

    # Merge (boundary-heading dedup; signature matches production parser)
    merged_lines = merge_shard_lines(all_shard_lines)

    # Always copy shard images into the package root. Unlike production's MinerU
    # fast path (which writes directly to output_dir), this debug script always
    # stages MinerU under ``_shards/shard_*`` — even for the 1-shard/no-TOC case.
    # Skipping merge_images on fast_path left ``text_track/images/`` empty and
    # dropped all image chunks in Phase B.
    merge_images(shard_output_dirs, str(out_dir))

    total_headings = sum(1 for line in merged_lines if line.startswith("#"))
    logger.info("   📎 Merged: {} lines, {} headings", len(merged_lines), total_headings)

    _write_json(work_dir / "merged_lines.json", merged_lines)

    # Output the full hierarchy tree as a structured JSON
    hierarchy_tree = _build_hierarchy_tree(merged_lines)
    _write_json(out_dir / "hierarchy.json", hierarchy_tree)
    logger.info("   🌲 Hierarchy tree: {} top-level nodes, {} total nodes",
                len(hierarchy_tree), _count_tree_nodes(hierarchy_tree))

    elapsed = time.time() - t0
    return merged_lines, elapsed


def _build_hierarchy_tree(merged_lines: list[str]) -> dict[str, Any]:
    """Build a nested hierarchy dict from merged lines (same format as manifest HIERARCHY).

    Sibling titles that collide get the same ``_2`` / ``_3`` … suffix used by
    markdown ``ParseState.enter_heading`` / chunk paths, so duplicate names
    (e.g. repeated ``临床实践要点：``) remain visible in the tree.
    """
    from shared.services.chunks.path_segments import (
        DOCUMENT_PATH_SEP,
        escape_path_segment,
    )

    headings: list[tuple[int, str]] = []
    for line in merged_lines:
        if line.startswith("#"):
            level = 0
            for ch in line:
                if ch == "#":
                    level += 1
                else:
                    break
            title = line[level:].strip()
            headings.append((level, title))

    if not headings:
        return {}

    root: dict[str, Any] = {}
    # (level, node_dict, escaped_disambiguated_title)
    stack: list[tuple[int, dict[str, Any], str]] = []
    path_counter: dict[str, int] = {}

    for level, title in headings:
        node: dict[str, Any] = {}
        while stack and stack[-1][0] >= level:
            stack.pop()

        current_heading = escape_path_segment(title)
        parent_names = [item_title for _, _, item_title in stack]
        tentative_path = DOCUMENT_PATH_SEP.join([*parent_names, current_heading])
        if tentative_path in path_counter:
            path_counter[tentative_path] += 1
            current_heading = f"{current_heading}_{path_counter[tentative_path]}"
        else:
            path_counter[tentative_path] = 1

        parent = stack[-1][1] if stack else root
        parent[current_heading] = node
        stack.append((level, node, current_heading))

    return root


def _count_tree_nodes(tree: dict[str, Any]) -> int:
    count = 0
    for _key, children in tree.items():
        count += 1
        if children:
            count += _count_tree_nodes(children)
    return count


def _stage_hierarchy_docx(
    file_path: str,
    filename: str,
    out_dir: Path,
    model: str | None,
) -> tuple[Any, float]:
    from app.services.document_parser.formats.docx.parser import parse_docx

    logger.info("=" * 70)
    logger.info("🔬 Stage 2: DOCX parsing (parse_docx)")
    logger.info("=" * 70)

    t0 = time.time()
    base_llm_paras = {
        "smart_title_parse": True,
        "summary_image": True,
        "summary_table": True,
        "summary_txt": True,
        "stopwords": [],
        "model_name": model or os.environ.get("NORMOL_MODEL"),
    }
    # relative_root must be the document file name (production contract),
    # never the absolute debug out_dir — otherwise chunk.path / section trees
    # leak ~/.knowhere/_debug_parse/.../text_track into retrieval.
    parsed_df = parse_docx(
        file_path,
        base_llm_paras,
        str(out_dir),
        filename,
        file_url="",
        relative_root=filename,
    )
    elapsed = time.time() - t0
    logger.info("   DOCX parsed in {:.1f}s → {} rows", elapsed, len(parsed_df) if parsed_df is not None else 0)
    return parsed_df, elapsed


def _stage_hierarchy_md(
    file_path: str,
    filename: str,
    out_dir: Path,
    model: str | None,
) -> tuple[Any, float]:
    from app.services.document_parser.formats.markdown.parser import parse_md

    logger.info("=" * 70)
    logger.info("🔬 Stage 2: Markdown parsing (parse_md)")
    logger.info("=" * 70)

    t0 = time.time()
    base_llm_paras = {
        "smart_title_parse": True,
        "summary_image": True,
        "summary_table": True,
        "summary_txt": True,
        "stopwords": [],
        "model_name": model or os.environ.get("NORMOL_MODEL"),
    }
    parsed_df = parse_md(
        str(out_dir),
        source_type="md",
        file_path=file_path,
        base_llm_paras=base_llm_paras,
        relative_root=filename,
    )
    elapsed = time.time() - t0
    logger.info("   MD parsed in {:.1f}s → {} rows", elapsed, len(parsed_df) if parsed_df is not None else 0)
    return parsed_df, elapsed


# ── Stage 4: Full Extraction → Chunks ──────────────────────────────────────

def _stage_full_pdf(
    out_dir: Path,
    filename: str,
    merged_lines: list[str],
    model: str | None,
):
    from app.services.document_parser.formats.markdown.parser import parse_md
    from app.services.document_parser.orchestration.postprocess import apply_parse_postprocess

    logger.info("=" * 70)
    logger.info("📦 Stage 4: parse_md Phase B → DataFrame → chunks")
    logger.info("=" * 70)

    t0 = time.time()
    base_llm_paras = {
        "smart_title_parse": True,
        "summary_image": True,
        "summary_table": True,
        "summary_txt": True,
        "stopwords": [],
        "model_name": model or os.environ.get("NORMOL_MODEL"),
    }
    parsed_df = parse_md(
        str(out_dir),
        source_type="md",
        base_llm_paras=base_llm_paras,
        relative_root=filename,
        lines_with_heading=merged_lines,
    )
    parsed_df = apply_parse_postprocess(str(out_dir), parsed_df)
    elapsed_parse = time.time() - t0
    logger.info("   parse_md Phase B done in {:.1f}s → {} rows", elapsed_parse, len(parsed_df) if parsed_df is not None else 0)

    return _finalize_df(out_dir, filename, parsed_df)


def _finalize_df(out_dir: Path, filename: str, parsed_df):
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder
    from datetime import datetime, timezone

    t0 = time.time()
    chunks = dataframe_to_chunks(parsed_df)
    logger.info("   {} chunks generated", len(chunks))

    _write_json(out_dir / "chunks.json", {"chunks": chunks})

    doc_nav = ZipDocNavigationBuilder().build_doc_nav(chunks, filename)
    _write_json(out_dir / "doc_nav.json", doc_nav)

    manifest = {
        "version": "2.0",
        "job_id": filename,
        "source_file_name": filename,
        "processing_date": datetime.now(timezone.utc).isoformat(),
        "processing": {
            "token_usage": get_current_token_tracker(),
        },
        "statistics": doc_nav.get("stats", {}),
    }
    _write_json(out_dir / "manifest.json", manifest)

    try:
        from app.services.connect_builder.summary_builder import enrich_doc_nav_summaries
        enrich_doc_nav_summaries(str(out_dir.parent), source_file=filename, use_llm=False)
    except Exception as exc:
        logger.warning("   enrich failed (non-fatal): {}", exc)

    elapsed = time.time() - t0
    logger.info("   finalize done in {:.1f}s", elapsed)
    return chunks, elapsed


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Staged text-track debug: profile → mineru → hierarchy → full"
        ),
    )
    parser.add_argument("--file", default=None, help="Input file path (PDF/DOCX/MD)")
    parser.add_argument("--model", default=None, help="Override LLM model")
    parser.add_argument(
        "--stop-at",
        choices=["profile", "mineru", "hierarchy", "full"],
        default="full",
        help="Pipeline stopping point (default: full)",
    )
    parser.add_argument("--reuse-profile", action="store_true",
                        help="Reuse cached _doc_agent/anatomy_map.json")
    parser.add_argument("--reuse-mineru", action="store_true",
                        help="Reuse cached shard dirs (skip MinerU extraction)")
    parser.add_argument("--reuse-hierarchy", action="store_true",
                        help="Reuse cached merged_lines.json (skip heading prediction)")
    parser.add_argument("--sjsyj", action="store_true",
                        help=f"Use fixture: {DEFAULT_SJSYJ_PDF}")
    parser.add_argument("--spacex", action="store_true",
                        help=f"Use fixture: {DEFAULT_SPACEX_PDF}")
    parser.add_argument("--run-db", action="store_true",
                        help="Publish to DB after full extraction")
    parser.add_argument("--clean", action="store_true",
                        help="Delete existing output before running")
    args = parser.parse_args()

    # Resolve input file
    file_path: str | None = args.file
    if args.sjsyj:
        file_path = str(DEFAULT_SJSYJ_PDF)
    elif args.spacex:
        file_path = str(DEFAULT_SPACEX_PDF)

    if not file_path:
        parser.error("Provide --file, --sjsyj, or --spacex")

    file_path = str(Path(file_path).expanduser().resolve())
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    filename = os.path.basename(file_path)
    ext = Path(file_path).suffix.lower()

    from app.services.document_parser.orchestration.path_segment import build_parser_path_segment
    dir_name = build_parser_path_segment(filename)
    out_dir = OUTPUT_ROOT / dir_name / "text_track"

    if args.clean and out_dir.exists():
        logger.info("🗑️  Cleaning {}", out_dir)
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    init_token_tracker()
    ledger = _load_token_ledger(out_dir)
    prev_usage = deepcopy(get_current_token_tracker() or empty_token_usage())

    logger.info("█" * 70)
    logger.info("  TEXT-TRACK DEBUG: {}", filename)
    logger.info("  OUTPUT: {}", out_dir)
    logger.info("  STOP-AT: {}", args.stop_at)
    logger.info("█" * 70)

    trace: dict[str, Any] = {
        "file": filename,
        "format": ext,
        "stop_at": args.stop_at,
        "stages": {},
    }
    t_start = time.time()

    try:
        # ── Format dispatch ──────────────────────────────────────────────────
        if ext == ".pdf":
            # Stage 1: Profile
            if args.reuse_profile:
                anatomy = _load_anatomy_cache(out_dir, file_path, filename)
            else:
                anatomy, profile_elapsed, profile_meta = _stage_profile(
                    file_path, filename, out_dir, args.model
                )
                trace["stages"]["profile"] = {
                    "elapsed_s": round(profile_elapsed, 1),
                    **profile_meta,
                }
                prev_usage = _record_token_stage(
                    ledger, "profile", prev=prev_usage, out_dir=out_dir
                )

            if args.stop_at == "profile":
                trace["stages"].setdefault("profile", {})["shard_count"] = len(
                    anatomy.shard_plan.shards
                )
                logger.info("⏸️  Stopped at profile → {}", out_dir)
                return 0

            # Stage 2: MinerU extraction
            if not args.reuse_mineru:
                _shard_dirs, mineru_elapsed = _stage_mineru_pdf(
                    file_path, filename, out_dir, anatomy,
                )
                trace["stages"]["mineru"] = {
                    "elapsed_s": round(mineru_elapsed, 1),
                    "shard_count": len(_shard_dirs),
                }
                prev_usage = _record_token_stage(
                    ledger, "mineru", prev=prev_usage, out_dir=out_dir
                )
            else:
                logger.info("⏩ Reusing cached MinerU shard dirs")

            if args.stop_at == "mineru":
                logger.info("⏸️  Stopped at mineru → {}", out_dir)
                return 0

            # Stage 3: Heading prediction + hierarchy tree
            if args.reuse_hierarchy:
                merged_path = out_dir / "_shards" / "merged_lines.json"
                if not merged_path.exists():
                    raise FileNotFoundError(f"No cached hierarchy: {merged_path}")
                logger.info("⏩ Reusing cached merged_lines: {}", merged_path)
                merged_lines = json.loads(merged_path.read_text(encoding="utf-8"))
            else:
                merged_lines, hier_elapsed = _stage_hierarchy_pdf(
                    out_dir, anatomy, args.model,
                )
                trace["stages"]["hierarchy"] = {
                    "elapsed_s": round(hier_elapsed, 1),
                    "merged_lines_count": len(merged_lines),
                    "heading_count": sum(1 for ln in merged_lines if ln.startswith("#")),
                }
                prev_usage = _record_token_stage(
                    ledger, "hierarchy", prev=prev_usage, out_dir=out_dir
                )

            if args.stop_at == "hierarchy":
                logger.info("⏸️  Stopped at hierarchy → {}", out_dir)
                return 0

            # Stage 4: Full extraction
            chunks, full_elapsed = _stage_full_pdf(
                out_dir, filename, merged_lines, args.model
            )
            trace["stages"]["full"] = {
                "elapsed_s": round(full_elapsed, 1),
                "chunk_count": len(chunks),
            }
            prev_usage = _record_token_stage(
                ledger, "full", prev=prev_usage, out_dir=out_dir
            )

        elif ext in (".docx", ".doc"):
            if args.stop_at in ("profile", "mineru"):
                logger.info("ℹ️  No profiling/MinerU for DOCX format. Nothing to do.")
                return 0

            parsed_df, hier_elapsed = _stage_hierarchy_docx(
                file_path, filename, out_dir, args.model
            )
            trace["stages"]["hierarchy"] = {
                "elapsed_s": round(hier_elapsed, 1),
                "row_count": len(parsed_df) if parsed_df is not None else 0,
            }
            prev_usage = _record_token_stage(
                ledger, "hierarchy", prev=prev_usage, out_dir=out_dir
            )

            if args.stop_at == "hierarchy":
                logger.info("⏸️  Stopped at hierarchy → {}", out_dir)
                return 0

            from app.services.document_parser.orchestration.postprocess import (
                apply_parse_postprocess,
            )
            parsed_df = apply_parse_postprocess(str(out_dir), parsed_df)
            chunks, full_elapsed = _finalize_df(out_dir, filename, parsed_df)
            trace["stages"]["full"] = {
                "elapsed_s": round(full_elapsed, 1),
                "chunk_count": len(chunks),
            }
            prev_usage = _record_token_stage(
                ledger, "full", prev=prev_usage, out_dir=out_dir
            )

        elif ext in (".md", ".markdown"):
            if args.stop_at in ("profile", "mineru"):
                logger.info("ℹ️  No profiling/MinerU for Markdown format. Nothing to do.")
                return 0

            parsed_df, hier_elapsed = _stage_hierarchy_md(
                file_path, filename, out_dir, args.model
            )
            trace["stages"]["hierarchy"] = {
                "elapsed_s": round(hier_elapsed, 1),
                "row_count": len(parsed_df) if parsed_df is not None else 0,
            }
            prev_usage = _record_token_stage(
                ledger, "hierarchy", prev=prev_usage, out_dir=out_dir
            )

            if args.stop_at == "hierarchy":
                logger.info("⏸️  Stopped at hierarchy → {}", out_dir)
                return 0

            from app.services.document_parser.orchestration.postprocess import (
                apply_parse_postprocess,
            )
            parsed_df = apply_parse_postprocess(str(out_dir), parsed_df)
            chunks, full_elapsed = _finalize_df(out_dir, filename, parsed_df)
            trace["stages"]["full"] = {
                "elapsed_s": round(full_elapsed, 1),
                "chunk_count": len(chunks),
            }
            prev_usage = _record_token_stage(
                ledger, "full", prev=prev_usage, out_dir=out_dir
            )

        else:
            logger.error("Unsupported format: {}", ext)
            return 1

        # ── Optional DB publication ──────────────────────────────────────────
        if args.run_db:
            from scripts._debug_publish import publish_debug_result_dir
            publish_result = publish_debug_result_dir(
                result_dir=out_dir,
                source_file_name=filename,
                chunks=chunks,
                parse_track="text_track",
                upload_assets=True,
            )
            trace["stages"]["db_publish"] = {
                "job_id": publish_result.job_id,
                "document_id": publish_result.document_id,
            }

        trace["total_elapsed_s"] = round(time.time() - t_start, 1)
        logger.info("")
        logger.info("═" * 70)
        logger.info("  ✅ DONE in {:.1f}s → {}", time.time() - t_start, out_dir)
        logger.info("═" * 70)
        return 0
    finally:
        remainder = token_usage_delta(
            prev_usage,
            deepcopy(get_current_token_tracker() or {}),
        )
        usage = _aggregate_token_ledger(
            ledger,
            remainder=remainder if remainder.get("calls") or remainder.get("total_tokens") else None,
        )
        _apply_token_usage_to_outputs(out_dir, trace, usage)
        cleanup_token_tracker()


if __name__ == "__main__":
    raise SystemExit(main())
