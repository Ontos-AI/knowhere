# pyright: reportArgumentType=false
import os

from app.services.document_parser.formats.markdown.parser import parse_md
from app.services.document_parser.providers.mineru.pdf_service import parse_via_full
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger

from shared.core.config import settings


def parse_pdfs(
    pdf_path,
    filename,
    output_dir,
    base_llm_paras,
    profile=None,
    relative_root=None,
    s3_key=None,
):
    route = profile.route if profile else "standard"
    base_llm_paras.update({"doc_name": filename})

    # ── Atlas routing: bypass MinerU entirely ──
    if profile and profile.doc_category == "atlas":
        logger.info(f"📐 Atlas detected, bypassing MinerU for {filename}")
        from app.services.document_parser.formats.atlas.parser import parse_atlas

        return parse_atlas(
            pdf_path, output_dir, base_llm_paras, relative_root, profile=profile
        )

    # ── Oversized PDF: doc_agent → shard → parallel MinerU → merge → parse_md ──
    if profile and profile.page_count > settings.MAX_PDF_PAGE_LIMIT:
        logger.info(
            f"📄 Oversized PDF: {profile.page_count} pages > "
            f"{settings.MAX_PDF_PAGE_LIMIT} limit, entering shard pipeline"
        )
        return _parse_oversized_pdf(
            pdf_path, filename, output_dir, base_llm_paras,
            profile=profile, relative_root=relative_root, s3_key=s3_key,
        )

    # ── Standard single-pass MinerU ──
    logger.info(f"📄 Standard MinerU parse for {filename} [route={route}]")
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


def _parse_oversized_pdf(
    pdf_path, filename, output_dir, base_llm_paras,
    profile=None, relative_root=None, s3_key=None,
):
    """Handle PDFs exceeding MinerU's page limit via shard-first hierarchy.

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
    from app.services.document_parser.formats.pdf.shard_merger import merge_images
    from app.services.document_parser.formats.pdf.shard_splitter import (
        bin_pack_shards,
        run_doc_agent,
        split_pdf,
    )

    job_id = base_llm_paras.get("doc_name", filename)

    # 1. Run doc_agent to get full anatomy map (shard plan + TOC info)
    with stage_timer("pdf.doc_agent", filename=filename):
        anatomy = run_doc_agent(pdf_path, job_id=job_id, output_dir=output_dir)

    agent_shards = anatomy.shard_plan.shards

    # 2. Extract TOC info from anatomy for page exclusion and heading constraint
    toc_pages: set[int] = set()
    toc_hierarchies = None
    if anatomy.toc_result and anatomy.toc_result.toc_pages:
        toc_pages = set(anatomy.toc_result.toc_pages)
        toc_hierarchies = anatomy.toc_hierarchies
        logger.info(
            f"📌 DOC_AGENT TOC detected: {len(toc_pages)} pages to exclude "
            f"({sorted(toc_pages)}), "
            f"{len(toc_hierarchies) if toc_hierarchies else 0} hierarchy regions"
        )

    # 3. Bin-pack agent shards to maximize MinerU page limit
    merged_shards = bin_pack_shards(agent_shards, max_pages=settings.MAX_PDF_PAGE_LIMIT)
    logger.info(
        f"📦 Bin-packed {len(agent_shards)} agent shards → "
        f"{len(merged_shards)} MinerU shards"
    )
    for ms in merged_shards:
        logger.info(
            f"  shard_{ms.shard_index}: pages {ms.page_start}-{ms.page_end} "
            f"({ms.page_count} pages)"
        )

    # 4. Physically split PDF (exclude TOC pages if detected)
    work_dir = os.path.join(output_dir, "_shards")
    os.makedirs(work_dir, exist_ok=True)
    with stage_timer("pdf.split", filename=filename):
        shard_pdf_paths, _page_remap = split_pdf(
            pdf_path, merged_shards, work_dir,
            exclude_pages=toc_pages if toc_pages else None,
        )

    # 5. Parse each shard via MinerU (parallel)
    shard_output_dirs: list[str | None] = [None] * len(shard_pdf_paths)
    concurrency = settings.MINERU_SHARD_CONCURRENCY

    def _parse_single_shard(shard_idx, shard_pdf):
        shard_out = os.path.join(work_dir, f"shard_{shard_idx}_output")
        os.makedirs(shard_out, exist_ok=True)
        shard_filename = (
            f"{os.path.splitext(filename)[0]}_shard{shard_idx}.pdf"
        )
        logger.info(
            f"  🔄 MinerU shard_{shard_idx}: parsing"
        )
        parse_via_full(shard_pdf, shard_filename, shard_out, s3_key=None)
        return shard_out

    with stage_timer(
        "pdf.mineru_parallel", filename=filename, shard_count=len(shard_pdf_paths)
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
    hierarchy_model_name = (
        base_llm_paras.get("hierarchy_model_name")
        or base_llm_paras.get("model_name", settings.NORMOL_MODEL)
    )

    def _predict_shard_headings(shard_idx: int, shard_out_dir: str) -> ShardHeadingResult:
        """Run full heading prediction pipeline on a single shard's full.md."""
        md_path = os.path.join(shard_out_dir, "full.md")
        if not os.path.exists(md_path):
            logger.warning(f"shard_{shard_idx}: full.md not found, returning empty")
            return ShardHeadingResult(shard_index=shard_idx, lines_with_heading=[], heading_count=0)

        with open(md_path, "r", encoding="utf-8") as f:
            md_lines = f.readlines()
        md_lines = [line.strip() for line in md_lines if line.strip() != ""]
        md_lines = merge_html_tables(md_lines)

        # TOC context: first TOC shared by all shards; subsequent TOCs assigned
        # by page boundary. For simplicity, all TOCs are passed since pred_titles
        # only matches headings actually present in this shard's content.
        shard_toc = toc_hierarchies

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

    shard_heading_results: list[ShardHeadingResult | None] = [None] * len(shard_output_dirs)

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
    all_lines_with_heading: list[str] = []
    total_headings = 0
    for result in shard_heading_results:
        if result is not None:
            all_lines_with_heading.extend(result.lines_with_heading)
            total_headings += result.heading_count

    logger.info(
        f"📎 Merged {len(shard_heading_results)} shards: "
        f"{len(all_lines_with_heading)} lines, {total_headings} headings"
    )

    with stage_timer("pdf.merge_images", filename=filename):
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


