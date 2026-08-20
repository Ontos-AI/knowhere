#!/usr/bin/env python3
"""Unified production-style document parsing debug script.

Supports all chunk-track Knowhere formats (PDF, DOCX, XLSX, PPTX, MD, Image,
Fragment) through the same checkerboard parser entry used by the worker. Use
``debug_page_memory.py`` for page-track step debugging.

Pipeline stages:
  1. checkerboard_parse_output → DataFrame
  2. dataframe_to_chunks       → list[ChunkPayload]
  3. ZipResultService          → chunks.json / manifest.json / doc_nav.json / *.zip
  4. enrich_doc_nav            → summary enrichment + top_summary
  5. DB publication            → DocumentSection + DocumentChunk (optional, --run-db)

Output directory:
  default → ~/.knowhere/chengke_kb/<docname>/

Usage:
  cd apps/worker

  # All formats (full pipeline)
  python scripts/debug_parse.py --file /path/to/any.pdf
  python scripts/debug_parse.py --file /path/to/doc.docx
  python scripts/debug_parse.py --file /path/to/sheet.xlsx
  python scripts/debug_parse.py --fragment "粘贴的文本..."

  # Options
  python scripts/debug_parse.py --spacex --run-db              # Enable DB publication
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

# ── Bootstrap: path + env ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[3]
WORKER_ROOT = ROOT / "apps" / "worker"
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(ROOT / "packages" / "shared-python"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(WORKER_ROOT / ".env")
os.environ.setdefault("LOCAL_DEBUG", "1")
os.environ.setdefault("OVERSIZED_PDF_SHARD_ENABLED", "true")

from loguru import logger  # noqa: E402

from shared.core.config import settings  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_SPACEX_PDF = Path("/Users/wuchengke/Desktop/temp/test_docs/spacex-s1.pdf")
DEFAULT_SJSYJ_PDF = Path(
    "/Users/wuchengke/Desktop/temp/test_docs/"
    "SJSYJ-SC-2024 企业制度汇编（上册）.pdf"
)

PRODUCTION_OUTPUT_ROOT = Path("~/.knowhere/chengke_kb").expanduser()
PROFILE_TRANSIENT_DIRS = (
    "_doc_agent",
    "coarse_profile_pages",
    "calibration_scan",
    "calibration_verify",
    "toc_pages",
    "ocr_pages",
    "inspect_pages",
    "profile_visuals",
    # Legacy dirs from older runs.
    "agent_visuals",
    "planner_pages",
    "page_locate_pages",
    "verify_pages",
    "calibration_inspect",
)

# ══════════════════════════════════════════════════════════════════════════════
# Section A: DB Publication (Stage 10) — preserved from original debug_parse.py
# ══════════════════════════════════════════════════════════════════════════════

def _run_db_publication(
    chunks: list,
    add_dir: str,
    source_file_name: str,
):
    """Stage 10: publish an already finalized debug result to local DB/S3."""
    from scripts._debug_publish import publish_debug_result_dir

    result = publish_debug_result_dir(
        result_dir=add_dir,
        source_file_name=source_file_name,
        chunks=chunks,
        upload_assets=True,
    )
    logger.info("    ✅ DB transaction committed (job_id={})", result.job_id)


# ══════════════════════════════════════════════════════════════════════════════
# Section C: Common Post-Parse Pipeline (Stage 7-10)
# ══════════════════════════════════════════════════════════════════════════════

def _finalize_output(
    parsed_df,
    add_dir: str,
    source_file_name: str,
    *,
    run_db: bool = False,
    job_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stage 7-10: chunks → ZIP → enrich → optional DB.

    Mirrors production flow:
      - parse_result_package.py L49   → dataframe_to_chunks
      - success_finalization.py L196  → ZipResultService
      - success_finalization.py L126  → enrich_doc_nav_summaries
      - debug_parse.py _run_db_publication → DB write

    Returns the chunks list.
    """
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.storage.zip_result_service import ZipResultService
    from app.services.connect_builder.summary_builder import (
        build_section_summary_lookup,
        enrich_doc_nav_summaries,
        ensure_doc_nav_json,
        load_nav_top_summary,
    )

    timings: dict[str, float] = {}

    # ── Stage 7: DataFrame → chunks (mirrors parse_result_package.py L49) ──
    logger.info("=" * 60)
    logger.info("📦 Stage 7: dataframe_to_chunks")
    logger.info("=" * 60)

    t0 = time.time()
    chunks = dataframe_to_chunks(parsed_df)
    timings["Stage 7: chunks"] = time.time() - t0

    text_count = sum(1 for c in chunks if c.get("type") == "text")
    image_count = sum(1 for c in chunks if c.get("type") == "image")
    table_count = sum(1 for c in chunks if c.get("type") == "table")
    page_count = sum(1 for c in chunks if c.get("type") == "page")
    table_ref_count = sum(
        1
        for c in chunks
        if c.get("type") == "table"
        and str(c.get("content") or "").strip().startswith("tables/")
    )
    table_inline_html_count = sum(
        1
        for c in chunks
        if c.get("type") == "table"
        and "<table" in str(c.get("content") or "").lower()
    )
    logger.info(
        f"   Total: {len(chunks)} chunks "
        f"(text={text_count}, image={image_count}, "
        f"table={table_count}, page={page_count})"
    )
    if table_count:
        logger.info(
            "   Table schema: content_ref={}/{} inline_html={}",
            table_ref_count,
            table_count,
            table_inline_html_count,
        )
    _cleanup_agent_transient_dirs(add_dir)

    # ── Stage 8: Enrich doc_nav summaries (mirrors success_finalization.py L126-160) ──
    logger.info("=" * 60)
    logger.info("📝 Stage 8: enrich_doc_nav_summaries")
    logger.info("=" * 60)

    t0 = time.time()
    try:
        if os.path.exists(os.path.join(add_dir, "doc_nav.json")):
            logger.info("   doc_nav.json exists (from ZIP), skipping ensure")
        else:
            ensure_doc_nav_json(add_dir, chunks, source_file_name=source_file_name)
            logger.info("   doc_nav.json created via ensure_doc_nav_json")

        document_root = os.path.dirname(add_dir)
        enrich_doc_nav_summaries(
            document_root,
            source_file=source_file_name,
            use_llm=False,
            top_summary_use_llm=True,
            chunks=chunks,
        )

        section_summaries = build_section_summary_lookup(add_dir)
        logger.info(f"   Section summaries: {len(section_summaries)} entries")

        top_summary = load_nav_top_summary(add_dir, source_file_name)
        if top_summary:
            logger.info(f"   Top summary: {top_summary[:80]}...")
        else:
            logger.warning("   Top summary: (empty)")

    except Exception as exc:
        logger.warning(f"   ⚠️ Enrichment failed (non-fatal): {exc}")
    timings["Stage 8: enrich"] = time.time() - t0

    if job_metadata is not None:
        from app.services.document_parser.support.stage_profiler import (
            get_current_stage_tracker,
        )
        from shared.services.ai.token_tracking import get_current_token_tracker

        token_usage = get_current_token_tracker()
        timing_ms = get_current_stage_tracker()
        stages = dict(job_metadata.get("stages") or {})
        if token_usage is not None:
            stages["token_usage"] = dict(token_usage)
        if timing_ms is not None:
            stages["timing_ms"] = dict(timing_ms)
        job_metadata["stages"] = stages

    # ── Stage 9: ZIP package (mirrors success_finalization.py L196-219) ──
    logger.info("=" * 60)
    logger.info("📦 Stage 9: ZipResultService")
    logger.info("=" * 60)

    t0 = time.time()
    zip_service = ZipResultService()
    job_id = os.path.basename(add_dir)

    try:
        zip_file_path, checksum, statistics, zip_size = zip_service.generate_zip_package(
            job_id=job_id,
            chunks=chunks,
            add_dir=add_dir,
            source_file_name=source_file_name,
            data_id=None,
            job_metadata=job_metadata or {},
            parsed_df=parsed_df,
        )
        timings["Stage 9: ZIP"] = time.time() - t0
        logger.info(f"   ZIP: {zip_file_path}")
        logger.info(f"   Size: {zip_size / 1024:.2f} KB")
        logger.info(f"   Stats: {statistics}")

        # Extract key files from ZIP (mirrors debug_parse.py ZIP extraction)
        with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
            for file_name in ["chunks.json", "manifest.json", "doc_nav.json"]:
                if file_name in zip_ref.namelist():
                    zip_ref.extract(file_name, add_dir)
                    logger.info(
                        f"   Extracted: {os.path.join(add_dir, file_name)}"
                    )

        local_zip_path = os.path.join(add_dir, f"{source_file_name}.zip")
        shutil.copy(zip_file_path, local_zip_path)
        logger.info(f"   ZIP copied → {local_zip_path}")

    except Exception as exc:
        timings["Stage 9: ZIP"] = time.time() - t0
        logger.error(f"   ❌ ZIP generation failed: {exc}")
        chunks_path = os.path.join(add_dir, "chunks.json")
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump({"chunks": chunks}, f, ensure_ascii=False, indent=2)
        logger.info(f"   Fallback: chunks.json saved → {chunks_path}")

    # ── Stage 10 (optional): DB publication ──
    if run_db:
        logger.info("=" * 60)
        logger.info("💾 Stage 10: DB publication")
        logger.info("=" * 60)

        t0 = time.time()
        try:
            _run_db_publication(
                chunks=chunks,
                add_dir=add_dir,
                source_file_name=source_file_name,
            )
        except Exception as exc:
            logger.warning(
                f"   ⚠️ DB publication failed (non-fatal): {exc}", exc_info=True
            )
        timings["Stage 10: DB"] = time.time() - t0
    else:
        logger.info("   ℹ️ Stage 10 skipped (use --run-db to enable)")

    # ── Timeline summary ──
    t_total = sum(timings.values())
    if t_total > 0:
        logger.info("")
        logger.info("═" * 58)
        logger.info("  📊 POST-PARSE TIMELINE")
        logger.info("═" * 58)
        for phase, elapsed in timings.items():
            pct = elapsed / t_total * 100
            logger.info(f"  {phase:<35s} │ {elapsed:>7.2f}s  ({pct:>5.1f}%)")
        logger.info("  " + "─" * 55)
        logger.info(f"  {'TOTAL':<35s} │ {t_total:>7.2f}s  (100.0%)")
        logger.info("═" * 58)

    return chunks


def _cleanup_agent_transient_dirs(add_dir: str) -> None:
    """Remove VLM render caches before packaging debug output."""
    removed: list[str] = []
    for dirname in PROFILE_TRANSIENT_DIRS:
        path = os.path.join(add_dir, dirname)
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(dirname)
    nested_doc_agent = os.path.join(add_dir, "_doc_agent")
    if os.path.isdir(nested_doc_agent):
        for dirname in PROFILE_TRANSIENT_DIRS:
            path = os.path.join(nested_doc_agent, dirname)
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(f"_doc_agent/{dirname}")
        if not os.listdir(nested_doc_agent):
            os.rmdir(nested_doc_agent)
            removed.append("_doc_agent")
    if removed:
        logger.info(f"   Cleaned transient agent dirs: {', '.join(removed)}")


# ══════════════════════════════════════════════════════════════════════════════
# Section D: Pipeline Entry Points
# ══════════════════════════════════════════════════════════════════════════════

def _run_standard_pipeline(
    file_path: str,
    source_file_name: str,
    output_root: str,
    *,
    run_db: bool = False,
    fragment_content: str = "",
) -> dict[str, Any]:
    """Standard pipeline for all formats: checkerboard_parse_output → finalize.

    Uses the production black-box entry point. Handles all formats including
    oversized PDFs (which are routed internally by parse_pdfs).

    Token/time tracking mirrors production parse_execution.py exactly:
    init trackers → parse → collect stats → cleanup.
    """
    from app.services.document_parser.parse_service import checkerboard_parse_output
    from app.services.document_parser.support.stage_profiler import (
        init_stage_tracker,
        cleanup_stage_tracker,
        get_current_stage_tracker,
    )
    from shared.services.ai.token_tracking import (
        init_token_tracker,
        cleanup_token_tracker,
        get_current_token_tracker,
    )

    filename = source_file_name
    is_fragment = ".fragment" in file_path.lower()

    logger.info("=" * 60)
    logger.info(f"📄 Standard pipeline: {filename}")
    logger.info(f"   Output root: {output_root}")
    logger.info("=" * 60)

    # ── Init trackers (same as parse_execution.py; reuse run_pipeline tracker) ──
    token_usage_dict = get_current_token_tracker()
    owns_token_tracker = token_usage_dict is None
    if token_usage_dict is None:
        token_usage_dict = init_token_tracker()

    stage_timing_dict = get_current_stage_tracker()
    owns_stage_tracker = stage_timing_dict is None
    if stage_timing_dict is None:
        stage_timing_dict = init_stage_tracker()

    try:
        t0 = time.time()
        result = checkerboard_parse_output(
            file_full_path=file_path,
            filename=filename,
            output_dir=output_root,
            internal_output_filename=filename,
            smart_title_parse=True,
            summary_image=True,
            summary_table=True,
            summary_txt=True,
            doc_type="auto",
            fragment_content=fragment_content if is_fragment else "",
        )
        parse_elapsed = time.time() - t0

        # ── Snapshot stats before cleanup ──
        stages_snapshot = {
            "timing_ms": dict(stage_timing_dict),
            "token_usage": dict(token_usage_dict),
        }
    finally:
        if owns_token_tracker:
            cleanup_token_tracker()
        if owns_stage_tracker:
            cleanup_stage_tracker()

    add_dir = result.output_dir
    parsed_df = result.parsed_df

    logger.info("=" * 60)
    logger.info(f"✅ Parse complete in {parse_elapsed:.1f}s")
    logger.info(f"   Output path: {add_dir}")
    if parsed_df is not None:
        logger.info(f"   DataFrame rows: {len(parsed_df)}")
    logger.info("=" * 60)

    # ── Print consumption stats ──
    logger.info("")
    logger.info("═" * 58)
    logger.info("  📊 CONSUMPTION STATS (mirrors manifest.processing.stages)")
    logger.info("═" * 58)
    token_usage = stages_snapshot["token_usage"]
    logger.info(
        f"  Token usage: prompt={token_usage['prompt_tokens']}, "
        f"completion={token_usage['completion_tokens']}, "
        f"total={token_usage['total_tokens']}"
    )
    timing_ms = stages_snapshot["timing_ms"]
    if timing_ms:
        logger.info("  Stage timings:")
        for stage, ms in sorted(timing_ms.items()):
            logger.info(f"    {stage:<45s} │ {ms:>8,}ms")
    else:
        logger.info("  Stage timings: (none recorded)")
    logger.info("═" * 58)

    if not add_dir or not os.path.exists(add_dir) or parsed_df is None or parsed_df.empty:
        logger.error("❌ Parse returned empty result, cannot proceed")
        return {"status": "error", "parse_elapsed": parse_elapsed}

    # Build job_metadata matching production parse_execution.py + success_finalization.py
    from datetime import datetime, timezone

    processing_completed_at = datetime.now(timezone.utc)
    debug_job_metadata: dict[str, Any] = {
        "stages": stages_snapshot,
        "processing_started_at": processing_completed_at.isoformat(),
        "processing_completed_at": processing_completed_at.isoformat(),
        "processing_duration_ms": int(parse_elapsed * 1000),
    }

    try:
        # Stage 7-10
        chunks = _finalize_output(
            parsed_df, add_dir, source_file_name,
            run_db=run_db,
            job_metadata=debug_job_metadata,
        )
    finally:
        debug_job_metadata["stages"] = {
            "timing_ms": dict(stage_timing_dict),
            "token_usage": dict(token_usage_dict),
        }

    return {
        "status": "success",
        "parse_elapsed": round(parse_elapsed, 1),
        "output_dir": add_dir,
        "chunks_count": len(chunks) if chunks else 0,
        "stages": stages_snapshot,
    }



def run_pipeline(
    file_path: str,
    source_file_name: str,
    *,
    run_db: bool = False,
    fragment_content: str = "",
    output_root_override: str | None = None,
) -> dict[str, Any]:
    """Unified production-style E2E parser entry point."""
    from app.services.document_parser.support.stage_profiler import (
        cleanup_stage_tracker,
        get_current_stage_tracker,
        init_stage_tracker,
    )
    from shared.services.ai.token_tracking import (
        cleanup_token_tracker,
        get_current_token_tracker,
        init_token_tracker,
    )

    owns_token_tracker = get_current_token_tracker() is None
    if owns_token_tracker:
        init_token_tracker()

    owns_stage_tracker = get_current_stage_tracker() is None
    if owns_stage_tracker:
        init_stage_tracker()

    try:
        if output_root_override:
            output_root = output_root_override
        else:
            output_root = str(PRODUCTION_OUTPUT_ROOT)

        return _run_standard_pipeline(
            file_path,
            source_file_name,
            output_root,
            run_db=run_db,
            fragment_content=fragment_content,
        )
    finally:
        if owns_token_tracker:
            cleanup_token_tracker()
        if owns_stage_tracker:
            cleanup_stage_tracker()


# ══════════════════════════════════════════════════════════════════════════════
# Section E: CLI
# ══════════════════════════════════════════════════════════════════════════════

def test_config():
    """Print current configuration."""
    logger.info("=== Current Config ===")
    logger.info(f"ENVIRONMENT: {getattr(settings, 'ENVIRONMENT', 'N/A')}")
    logger.info(
        f"DATABASE_URL: {getattr(settings, 'DATABASE_URL', 'N/A')[:50]}..."
    )
    logger.info(f"REDIS_HOST: {getattr(settings, 'REDIS_HOST', 'N/A')}")
    logger.info(
        f"DS_KEY: {'set' if getattr(settings, 'DS_KEY', None) else 'unset'}"
    )
    logger.info(
        f"ALI_API_KEYS: {'set' if getattr(settings, 'ALI_API_KEYS', None) else 'unset'}"
    )
    logger.info(f"IMAGE_MODEL: {getattr(settings, 'IMAGE_MODEL', 'N/A')}")
    logger.info(f"NORMOL_MODEL: {getattr(settings, 'NORMOL_MODEL', 'N/A')}")
    logger.info(
        f"HIERARCHY_LLM_MODEL: {getattr(settings, 'HIERARCHY_LLM_MODEL', 'N/A')}"
    )
    logger.info(
        f"MAX_PDF_PAGE_LIMIT: {getattr(settings, 'MAX_PDF_PAGE_LIMIT', 'N/A')}"
    )


def _parse_cases(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Parse CLI args into [(name, file_path, fragment_content), ...].

    Returns a list of tuples: (job_name, file_path, fragment_content).
    For non-fragment cases, fragment_content is "".
    """
    cases: list[tuple[str, str, str]] = []

    for raw_case in args.case or []:
        if "=" not in raw_case:
            raise ValueError("--case must use name=/path/to/file")
        name, path = raw_case.split("=", 1)
        resolved = str(Path(path).expanduser().resolve())
        cases.append((name.strip(), resolved, ""))

    if args.file:
        file_path = str(Path(args.file).expanduser().resolve())
        job_id = args.job_id or Path(args.file).stem
        cases.append((job_id, file_path, ""))

    if args.fragment:
        cases.append(("fragment", ".fragment", args.fragment))

    if args.spacex:
        cases.append(
            ("spacex-s1", str(DEFAULT_SPACEX_PDF.expanduser().resolve()), "")
        )
    if args.sjsyj:
        cases.append(
            ("sjsyj", str(DEFAULT_SJSYJ_PDF.expanduser().resolve()), "")
        )

    return cases


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified document parsing debug script — supports all Knowhere formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Full pipeline (any format)
  python scripts/debug_parse.py --file /path/to/doc.pdf
  python scripts/debug_parse.py --file /path/to/doc.docx
  python scripts/debug_parse.py --fragment "粘贴的文本..."

  # Optional DB publication
  python scripts/debug_parse.py --spacex --run-db
""",
    )

    # Input sources
    input_group = parser.add_argument_group("input")
    input_group.add_argument("--file", help="Path to file to parse (any format)")
    input_group.add_argument("--job-id", help="Job id override for --file")
    input_group.add_argument(
        "--fragment", help="Text content for fragment mode parsing"
    )
    input_group.add_argument(
        "--spacex",
        action="store_true",
        help=f"SpaceX S-1 fixture: {DEFAULT_SPACEX_PDF}",
    )
    input_group.add_argument(
        "--sjsyj",
        action="store_true",
        help=f"企业制度汇编 fixture: {DEFAULT_SJSYJ_PDF}",
    )
    input_group.add_argument(
        "--case",
        action="append",
        help="Named fixture: name=/path/to/file",
    )

    # Post-processing
    post_group = parser.add_argument_group("post-processing")
    post_group.add_argument(
        "--run-db",
        action="store_true",
        help="Enable Stage 10: DB publication (requires running database)",
    )

    # Output control
    output_group = parser.add_argument_group("output")
    output_group.add_argument(
        "--output-root",
        default=None,
        help=(
            "Override output root directory. Default: "
            f"{PRODUCTION_OUTPUT_ROOT}"
        ),
    )
    output_group.add_argument(
        "--clean",
        action="store_true",
        help="Delete existing output before running",
    )
    output_group.add_argument(
        "--test-config",
        action="store_true",
        help="Print current configuration and exit",
    )

    args = parser.parse_args()

    if args.test_config:
        test_config()
        return 0

    cases = _parse_cases(args)
    if not cases:
        parser.error(
            "provide --file, --fragment, --spacex, --sjsyj, or at least one --case"
        )

    summaries = []

    for name, file_path, fragment_content in cases:
        if file_path != ".fragment" and not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        source_file_name = (
            os.path.basename(file_path) if file_path != ".fragment" else ""
        )

        # Clean if requested
        if args.clean:
            from app.services.document_parser.orchestration.path_segment import (
                build_parser_path_segment,
            )
            dir_name = build_parser_path_segment(source_file_name)
            clean_root = Path(args.output_root) if args.output_root else PRODUCTION_OUTPUT_ROOT
            clean_dir = clean_root / dir_name
            if clean_dir.exists():
                logger.info(f"🗑️  Cleaning {clean_dir}")
                shutil.rmtree(clean_dir)

        logger.info("")
        logger.info("█" * 60)
        logger.info(f"  CASE: {name}")
        logger.info(f"  FILE: {file_path}")
        logger.info("█" * 60)

        result = run_pipeline(
            file_path,
            source_file_name,
            run_db=args.run_db,
            fragment_content=fragment_content,
            output_root_override=args.output_root,
        )
        result["job_id"] = name
        summaries.append(result)

    # Final JSON summary
    print(json.dumps({"cases": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
