# pyright: reportArgumentType=false
import os
import re
import shutil

from app.services.document_parser.formats.markdown.parser import parse_md
from app.services.document_parser.orchestration.oversized_pdf_policy import (
    build_oversized_pdf_processing_failed_exception,
)
from app.services.document_parser.providers.mineru.pdf_service import parse_via_full
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
from app.services.document_parser.structure.toc_parser import detect_tocs_in_texts
from app.services.document_parser.support.stage_profiler import stage_timer
from app.services.document_parser.support.text_helpers import normalize_md
from loguru import logger

from shared.core.config import settings
from shared.services.storage.job_file_storage import JobFileStorage


def parse_pdfs(
    pdf_path,
    filename,
    output_dir,
    base_llm_paras,
    profile=None,
    relative_root=None,
    s3_key=None,
    job_id=None,
):
    base_llm_paras.update({"doc_name": filename})

    # ── Atlas routing: bypass MinerU entirely ──
    if profile and profile.routing_category is PdfRoutingCategory.ATLAS:
        logger.info(f"📐 Atlas detected, bypassing MinerU for {filename}")
        from app.services.document_parser.formats.atlas.parser import parse_atlas

        return parse_atlas(
            pdf_path, output_dir, base_llm_paras, relative_root, profile=profile
        )

    # ── Unified anatomy path: DOC_PROFILE → shard wrapper → parse_md Phase B ──
    if profile and getattr(profile, "anatomy", None) is not None:
        is_oversized = profile.page_count > settings.MAX_PDF_PAGE_LIMIT
        if is_oversized:
            logger.info(
                f"📄 Oversized PDF: {profile.page_count} pages > "
                f"{settings.MAX_PDF_PAGE_LIMIT} limit, entering shard pipeline"
            )
        else:
            logger.info(
                f"📄 Profile anatomy detected, entering PDF shard pipeline for {filename}"
            )
        try:
            return _parse_pdf_via_shards(
                pdf_path, filename, output_dir, base_llm_paras,
                profile=profile, relative_root=relative_root, s3_key=s3_key,
                job_id=job_id,
            )
        except Exception as exc:
            if not is_oversized:
                raise
            logger.exception(
                "Oversized PDF shard pipeline failed for {} (pages={})",
                filename,
                profile.page_count,
            )
            raise build_oversized_pdf_processing_failed_exception(
                page_count=profile.page_count,
                original_exception=exc,
            ) from exc

    # ── Standard single-pass MinerU ──
    logger.info(f"📄 Standard MinerU parse for {filename}")
    with stage_timer("pdf.extract.standard", filename=filename):
        parse_via_full(pdf_path, filename, output_dir, s3_key=s3_key)

    logger.info("✅ PDF parsing step 1 complete: text extracted")

    with stage_timer("pdf.parse_md", filename=filename):
        return parse_md(
            output_dir,
            source_type="md",
            file_path=os.path.join(output_dir, "full.md"),
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
        )


def _parse_pdf_via_shards(
    pdf_path, filename, output_dir, base_llm_paras,
    profile=None, relative_root=None, s3_key=None, job_id=None,
):
    """Handle PDFs via the unified shard-first hierarchy pipeline.

    Pipeline:
    1. DOC_AGENT → shard plan + TOC
    2. bin_pack → merged shards
    3. split_pdf (exclude TOC pages)
    4. MinerU per shard (parallel)
    5. **Per-shard heading prediction** (parallel)  ← NEW
    6. Merge lines_with_heading + images
    7. parse_md Phase B (skip TOC detection + heading prediction)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from dataclasses import dataclass

    from app.services.document_parser.formats.markdown.parser import (
        eval_md_headings,
        merge_html_tables,
    )
    from app.services.document_parser.formats.pdf.shard_merger import (
        merge_images,
        merge_shard_lines,
    )
    from app.services.document_parser.formats.pdf.shard_splitter import (
        bin_pack_shards,
        split_pdf,
    )

    work_dir: str | None = None
    temp_shard_s3_keys: list[str] = []

    try:
        # 1. Reuse the entry DOC_PROFILE anatomy map (shard plan + TOC info).
        anatomy = getattr(profile, "anatomy", None)
        if anatomy is None:
            raise RuntimeError(
                f"PDF profile for {filename} is missing structural anatomy"
            )
        if not anatomy.shard_plan.shards:
            raise RuntimeError(
                f"PDF profile for {filename} did not produce a shard plan"
            )

        agent_shards = anatomy.shard_plan.shards

        # 2. Extract TOC info from anatomy for page exclusion and heading constraint
        toc_pages: set[int] = set()
        toc_hierarchies = anatomy.toc_hierarchies
        if anatomy.toc_result and anatomy.toc_result.toc_pages:
            toc_pages = set(anatomy.toc_result.toc_pages)
            logger.info(
                f"📌 DOC_AGENT TOC detected: {len(toc_pages)} pages to exclude "
                f"({sorted(toc_pages)}), "
                f"{len(toc_hierarchies) if toc_hierarchies else 0} hierarchy regions"
            )

        # 3. Bin-pack agent shards to maximize MinerU page limit
        merged_shards = bin_pack_shards(
            agent_shards,
            max_pages=settings.MAX_PDF_PAGE_LIMIT,
        )
        logger.info(
            f"📦 Bin-packed {len(agent_shards)} agent shards → "
            f"{len(merged_shards)} MinerU shards"
        )
        for ms in merged_shards:
            logger.info(
                f"  shard_{ms.shard_index}: pages {ms.page_start}-{ms.page_end} "
                f"({ms.page_count} pages)"
            )

        fast_path_original_pdf = len(merged_shards) == 1 and not toc_pages

        # 4. Parse via MinerU. The 1-shard/no-TOC case keeps the original
        # PDF/S3 object to avoid temporary split/upload churn.
        shard_output_dirs: list[str | None]
        concurrency = settings.MINERU_SHARD_CONCURRENCY

        if fast_path_original_pdf:
            logger.info("📄 Single shard without TOC pages; using original PDF fast path")
            with stage_timer("pdf.extract.single_shard_fast", filename=filename):
                parse_via_full(pdf_path, filename, output_dir, s3_key=s3_key)
            shard_output_dirs = [output_dir]
        else:
            # Physically split PDF when TOC pages must be excluded or multiple
            # MinerU requests are required.
            work_dir = os.path.join(output_dir, "_shards")
            os.makedirs(work_dir, exist_ok=True)
            with stage_timer("pdf.split", filename=filename):
                shard_pdf_paths, _page_remap = split_pdf(
                    pdf_path, merged_shards, work_dir,
                    exclude_pages=toc_pages if toc_pages else None,
                )

            if not shard_pdf_paths:
                raise RuntimeError(
                    f"PDF shard split for {filename} produced no shard PDFs"
                )

            temp_shard_s3_keys = [
                _build_temp_shard_s3_key(
                    source_s3_key=s3_key,
                    job_id=job_id,
                    filename=filename,
                    shard_index=shard_index,
                )
                for shard_index, _shard_pdf_path in enumerate(shard_pdf_paths)
            ]

            # 5. Parse each shard via MinerU (parallel)
            shard_output_dirs = [None] * len(shard_pdf_paths)

            def _parse_single_shard(shard_idx, shard_pdf):
                assert work_dir is not None
                shard_out = os.path.join(work_dir, f"shard_{shard_idx}_output")
                os.makedirs(shard_out, exist_ok=True)
                shard_filename = (
                    f"{os.path.splitext(filename)[0]}_shard{shard_idx}.pdf"
                )
                shard_s3_key = temp_shard_s3_keys[shard_idx]
                logger.info(
                    f"  🔄 MinerU shard_{shard_idx}: parsing via S3 URL "
                    f"({shard_s3_key})"
                )
                parse_via_full(
                    shard_pdf, shard_filename, shard_out, s3_key=shard_s3_key
                )
                return shard_out

            with stage_timer(
                "pdf.mineru_parallel",
                filename=filename,
                shard_count=len(shard_pdf_paths),
            ):
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = {
                        executor.submit(_parse_single_shard, i, shard_pdf_path): i
                        for i, shard_pdf_path in enumerate(shard_pdf_paths)
                    }
                    for future in as_completed(futures):
                        idx = futures[future]
                        shard_output_dirs[idx] = future.result()

        # 6. Per-shard heading prediction (parallel)
        @dataclass
        class ShardHeadingResult:
            shard_index: int
            lines_with_heading: list[str]
            heading_count: int

        smart_parse = base_llm_paras.get("smart_title_parse", True)
        toc_model_name = base_llm_paras.get("model_name", settings.NORMOL_MODEL)
        hierarchy_model_name = (
            base_llm_paras.get("hierarchy_model_name")
            or base_llm_paras.get("model_name", settings.NORMOL_MODEL)
        )

        def _predict_shard_headings(
            shard_idx: int,
            shard_out_dir: str,
        ) -> ShardHeadingResult:
            """Run full heading prediction pipeline on a single shard's full.md."""
            md_path = os.path.join(shard_out_dir, "full.md")
            if not os.path.exists(md_path):
                raise FileNotFoundError(f"shard_{shard_idx}: full.md not found")

            with open(md_path, "r", encoding="utf-8") as f:
                md_lines = f.readlines()
            md_lines = [line.strip() for line in md_lines if line.strip() != ""]
            md_lines = merge_html_tables(md_lines)

            is_first_shard = shard_idx == 0
            shard_toc = toc_hierarchies
            if shard_toc is None and is_first_shard and _md_has_toc_keyword(md_lines):
                logger.info(
                    f"📌 shard_{shard_idx}: TOC keyword found without profile TOC; "
                    "reusing markdown TOC detector"
                )
                shard_toc, md_lines = detect_tocs_in_texts(
                    md_lines,
                    model_name=toc_model_name,
                    hierarchy_model_name=hierarchy_model_name,
                )

            lines_with_heading = eval_md_headings(
                md_lines,
                source_type="md",
                toc_hierarchies=shard_toc,
                smart_parse=smart_parse,
                model_name=hierarchy_model_name,
                output_dir=shard_out_dir,
                layout_json_path=(
                    os.path.join(shard_out_dir, "layout.json")
                    if os.path.exists(os.path.join(shard_out_dir, "layout.json"))
                    else None
                ),
                is_first_shard=is_first_shard,
            )

            heading_count = sum(1 for line in lines_with_heading if line.startswith("#"))
            logger.info(
                f"  ✅ shard_{shard_idx}: {heading_count} headings identified "
                f"from {len(lines_with_heading)} lines"
            )
            return ShardHeadingResult(
                shard_index=shard_idx,
                lines_with_heading=lines_with_heading,
                heading_count=heading_count,
            )

        shard_heading_results: list[ShardHeadingResult | None] = [None] * len(
            shard_output_dirs
        )

        with stage_timer(
            "pdf.shard_headings", filename=filename, shard_count=len(shard_output_dirs)
        ):
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(_predict_shard_headings, i, shard_dir): i
                    for i, shard_dir in enumerate(shard_output_dirs)
                    if shard_dir is not None
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    shard_heading_results[idx] = future.result()

        # 7. Merge: concatenate lines_with_heading (in shard order) + merge images
        complete_heading_results: list[ShardHeadingResult] = []
        for index, result in enumerate(shard_heading_results):
            if result is None:
                raise RuntimeError(f"Missing heading result for shard_{index}")
            complete_heading_results.append(result)

        # Compute level offsets: continuation shards get shifted deeper.
        shard_offsets: list[int] = []
        for shard in agent_shards[: len(complete_heading_results)]:
            if shard.is_continuation:
                shard_offsets.append(max(shard.split_depth - 1, 0))
            else:
                shard_offsets.append(0)
        if any(offset > 0 for offset in shard_offsets):
            logger.info(f"📐 Shard level offsets: {shard_offsets}")

        all_lines_with_heading: list[str] = merge_shard_lines(
            [result.lines_with_heading for result in complete_heading_results],
            shard_offsets=shard_offsets,
        )
        total_headings = sum(
            1 for line in all_lines_with_heading if line.startswith("#")
        )

        logger.info(
            f"📎 Merged {len(complete_heading_results)} shards: "
            f"{len(all_lines_with_heading)} lines, {total_headings} headings"
        )

        with stage_timer("pdf.merge_images", filename=filename):
            if not fast_path_original_pdf:
                merge_images(shard_output_dirs, output_dir)

        logger.info("✅ Shard-first hierarchy complete, entering parse_md Phase B")

        # 8. parse_md Phase B only (skip TOC detection + heading prediction)
        with stage_timer("pdf.parse_md", filename=filename):
            return parse_md(
                output_dir,
                source_type="md",
                base_llm_paras=base_llm_paras,
                relative_root=relative_root,
                lines_with_heading=all_lines_with_heading,
            )
    finally:
        _cleanup_temp_shard_s3_assets(temp_shard_s3_keys)
        _cleanup_local_shard_workspace(work_dir)


def _md_has_toc_keyword(md_lines: list[str]) -> bool:
    toc_keywords = {"目录", "目次", "tableofcontents", "contents"}
    return any(normalize_md(line) in toc_keywords for line in md_lines)


def _build_temp_shard_s3_key(
    *,
    source_s3_key: str | None,
    job_id: str | None,
    filename: str,
    shard_index: int,
) -> str:
    owner_segment = _sanitize_temp_storage_segment(
        job_id or _source_key_stem(source_s3_key) or os.path.splitext(filename)[0]
    )
    return f"tmp/mineru-shards/{owner_segment}/shard_{shard_index}.pdf"


def _source_key_stem(source_s3_key: str | None) -> str | None:
    if not source_s3_key:
        return None
    key_name = os.path.basename(source_s3_key.rstrip("/"))
    stem, _extension = os.path.splitext(key_name)
    return stem or None


def _sanitize_temp_storage_segment(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip(".-")
    return normalized or "document"


def _cleanup_temp_shard_s3_assets(s3_keys: list[str]) -> None:
    if not s3_keys:
        return

    storage = JobFileStorage()
    for s3_key in s3_keys:
        try:
            deleted = storage.delete_upload_file(s3_key)
            if deleted:
                logger.info(f"Deleted temporary MinerU shard S3 object: {s3_key}")
            else:
                logger.debug(f"Temporary MinerU shard S3 object was absent: {s3_key}")
        except Exception as exc:
            logger.warning(
                f"Failed to delete temporary MinerU shard S3 object "
                f"{s3_key}: {exc}"
            )


def _cleanup_local_shard_workspace(work_dir: str | None) -> None:
    if not work_dir or not os.path.exists(work_dir):
        return
    try:
        shutil.rmtree(work_dir)
        logger.info(f"Deleted temporary MinerU shard workspace: {work_dir}")
    except Exception as exc:
        logger.warning(
            f"Failed to delete temporary MinerU shard workspace {work_dir}: {exc}"
        )
