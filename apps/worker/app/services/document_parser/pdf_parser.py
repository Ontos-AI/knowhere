# pyright: reportArgumentType=false
import json
import os
import re

from app.services.document_parser.md_parser import parse_md
from app.services.document_parser.mineru_pdf_service import parse_via_full
from app.services.document_parser.pymupdf_subprocess import worker
from app.services.document_parser.stage_profiler import stage_timer
from loguru import logger


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


def _inject_page_markers_pymupdf(pdf_path: str, output_dir: str) -> None:
    """Inject <!-- page N --> markers into full.md for pymupdf4llm fast path.

    Must run inside the same process that holds the PyMuPDF import.
    """
    import pymupdf

    md_path = os.path.join(output_dir, "full.md")
    if not os.path.exists(md_path):
        return

    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return

    with open(md_path, "r", encoding="utf-8") as f:
        md_lines = f.readlines()

    anchors = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1
        blocks = page.get_text("blocks")
        for block in blocks:
            if block[6] == 0:
                text = block[4].strip().split("\n")[0].strip()
                if text and len(text) >= 3:
                    anchors.append((text, page_num))
                    break

    doc.close()

    if not anchors:
        return

    insertions = []
    used_lines = set()

    for anchor_text, page_num in anchors:
        anchor_norm = re.sub(r"\s+", " ", anchor_text).strip()
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
        return

    insertions.sort(key=lambda x: x[0], reverse=True)
    for line_idx, page_num in insertions:
        md_lines.insert(line_idx, f"<!-- page {page_num} -->\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.writelines(md_lines)


# ─── Child-process workers (top-level for pickling) ─────────────────


@worker
def _fast_path_worker(queue, pdf_path, output_dir, image_dir):
    """Child process: pymupdf4llm extraction + page marker injection."""
    import pymupdf
    import pymupdf4llm

    doc = pymupdf.open(pdf_path)
    try:
        md_text = pymupdf4llm.to_markdown(
            doc,
            write_images=True,
            image_path=image_dir,
            image_format="png",
        )
    finally:
        doc.close()

    full_md_path = os.path.join(output_dir, "full.md")
    with open(full_md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    _inject_page_markers_pymupdf(pdf_path, output_dir)

    img_count = len([n for n in os.listdir(image_dir) if n.endswith(".png")])
    queue.put(
        {
            "ok": True,
            "md_chars": len(md_text),
            "image_count": img_count,
        }
    )


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

    # ── Atlas routing: bypass MinerU entirely, use PyMuPDF for per-page chunking ──
    if profile and profile.doc_category == "atlas":
        logger.info(f"📐 Atlas detected, bypassing MinerU for {filename}")
        from app.services.document_parser.atlas_parser import parse_atlas

        return parse_atlas(
            pdf_path, output_dir, base_llm_paras, relative_root, profile=profile
        )

    # TODO: Re-enable fast path after thorough debugging.
    # Conservative strategy: until the fast path (pymupdf4llm) is fully validated,
    # all non-atlas PDFs are forced to MinerU (standard route) regardless of what
    # DocProfiler recommends. The routing logic below is intentionally bypassed.
    #
    # Original fast-path block (keep for reference, do NOT delete):
    # if route == "fast":
    #     logger.info(f"⚡ Fast path: extracting with pymupdf4llm for {filename}")
    #
    #     os.makedirs(output_dir, exist_ok=True)
    #     image_dir = os.path.join(output_dir, "images")
    #     os.makedirs(image_dir, exist_ok=True)
    #
    #     with stage_timer("pdf.extract.fast", filename=filename):
    #         result = run_in_child_process(
    #             _fast_path_worker, pdf_path, output_dir, image_dir,
    #         )
    #     logger.info(
    #         f"⚡ Fast path done: {result['md_chars']} chars, "
    #         f"{result['image_count']} images"
    #     )
    # else:
    #     with stage_timer("pdf.extract.standard", filename=filename):
    #         parse_via_full(pdf_path, filename, output_dir, s3_key=s3_key)
    #         _inject_page_markers(output_dir)

    logger.info(
        f"🛡️ Conservative mode: forcing MinerU (standard) for {filename} [route={route}]"
    )
    with stage_timer("pdf.extract.standard", filename=filename):
        parse_via_full(pdf_path, filename, output_dir, s3_key=s3_key)

        # Inject page markers from MinerU layout.json
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
