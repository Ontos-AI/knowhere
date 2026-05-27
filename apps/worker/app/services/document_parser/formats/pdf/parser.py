# pyright: reportArgumentType=false
import json
import os
import re

from app.services.document_parser.formats.markdown.parser import parse_md
from app.services.document_parser.providers.mineru.pdf_service import parse_via_full
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger

from shared.core.config import settings


def _inject_page_markers(output_dir: str) -> None:
    """Inject <!-- page N --> markers into full.md using layout.json page info.

    Reads layout.json to find the first text content of each page,
    then searches for that text in full.md and inserts a page marker above it.

    If layout.json is not available (e.g. fast path without MinerU),
    this function does nothing gracefully.
    """
    layout_path = os.path.join(output_dir, "layout.json")
    md_path = os.path.join(output_dir, "full.md")

    if not os.path.exists(layout_path) or not os.path.exists(md_path):
        logger.debug("layout.json or full.md not found, skipping page marker injection")
        return

    try:
        with open(layout_path, "r", encoding="utf-8") as f:
            layout_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to read layout.json: {e}")
        return

    pdf_info = layout_data.get("pdf_info", [])
    if not pdf_info:
        return

    with open(md_path, "r", encoding="utf-8") as f:
        md_lines = f.readlines()

    # Build anchor map: {normalized_text: page_number (1-based)}
    # Use the first text span of each page's first para_block as anchor
    anchors = []  # list of (anchor_text, page_num)
    for page in pdf_info:
        page_idx = page.get("page_idx", 0)
        page_num = page_idx + 1  # 1-based page number

        # Find the first non-empty text content in this page
        anchor_text = None
        for block in page.get("para_blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    content = span.get("content", "").strip()
                    if content and len(content) >= 3:  # skip very short anchors
                        anchor_text = content
                        break
                if anchor_text:
                    break
            if anchor_text:
                break

        if anchor_text:
            anchors.append((anchor_text, page_num))

    if not anchors:
        logger.debug(
            "No anchor texts found in layout.json, skipping page marker injection"
        )
        return

    # Match anchors against md_lines and insert markers
    # Process from end to start so line indices don't shift
    insertions = []  # list of (line_index, page_num)
    used_lines = set()

    for anchor_text, page_num in anchors:
        # Normalize anchor for matching
        anchor_norm = re.sub(r"\s+", " ", anchor_text).strip()
        if len(anchor_norm) < 3:
            continue

        # Search for anchor in md_lines (use first 50 chars for substring match)
        search_key = anchor_norm[:50]
        for i, line in enumerate(md_lines):
            if i in used_lines:
                continue
            line_norm = re.sub(r"^#+\s*", "", line.strip())
            line_norm = re.sub(r"\s+", " ", line_norm).strip()
            if search_key in line_norm:
                insertions.append((i, page_num))
                used_lines.add(i)
                break

    if not insertions:
        logger.debug("No page marker matches found, skipping injection")
        return

    # Sort by line index descending to insert from bottom to top
    insertions.sort(key=lambda x: x[0], reverse=True)
    for line_idx, page_num in insertions:
        md_lines.insert(line_idx, f"<!-- page {page_num} -->\n")

    # Write back
    with open(md_path, "w", encoding="utf-8") as f:
        f.writelines(md_lines)

    logger.info(f"Injected {len(insertions)} page markers into full.md")



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
        _inject_page_markers(output_dir)

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
    """Handle PDFs exceeding MinerU's page limit via doc_agent shard-and-stitch."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.services.document_parser.formats.pdf.shard_merger import (
        merge_shard_outputs,
    )
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

    # 6. Merge all shard outputs into main output_dir
    with stage_timer("pdf.merge_shards", filename=filename):
        merge_shard_outputs(shard_output_dirs, merged_shards, output_dir)

    # 7. Inject page markers (uses merged layout.json with corrected page_idx)
    # Note: page markers may be inaccurate when TOC pages are excluded, but
    # this only affects chunk metadata (page_nums), not heading hierarchy.
    _inject_page_markers(output_dir)

    logger.info("✅ Oversized PDF shard-and-stitch complete, entering parse_md")

    # 8. Standard parse_md — pass DOC_AGENT TOC hierarchies to skip
    #    row-based TOC detection and enable hard-constraint heading assignment
    with stage_timer("pdf.parse_md", filename=filename):
        return parse_md(
            output_dir,
            source_type="md",
            file_path=os.path.join(output_dir, "full.md"),
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
            toc_hierarchies=toc_hierarchies,
        )

