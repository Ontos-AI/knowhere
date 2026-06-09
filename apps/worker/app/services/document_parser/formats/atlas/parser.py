# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""
Atlas-specific parsing pipeline.

For documents detected as atlas (category="atlas") — e.g. engineering drawing
collections — this module BYPASSES MinerU entirely and uses PyMuPDF directly to:
  1. Extract text from each page (for naming and content)
  2. Render each page as a single complete image (preserving full-page layout)
  3. For scanned atlases: auto-call VLM to extract drawing info from each page

MinerU is unsuitable for atlas because it fragments each page into multiple
sub-images (e.g. 700+ images for 135 pages), destroying per-page integrity.
"""

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from app.services.document_parser.tables.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.formats.pdf.pymupdf_subprocess import run_in_child_process, worker
from app.services.document_parser.structure.toc_parser import detect_tocs_in_texts
from loguru import logger

from shared.core.config import settings
from shared.utils.chunk_refs import build_chunk_ref
from shared.utils.text_utils import tokenize2stw_remove

# ─── Config ───────────────────────────────────────────────────────────
VLM_CONCURRENCY = 3  # concurrent VLM calls
IMG_RENDER_DPI = 150  # DPI for full-page rendering
IMG_MAX_SIDE = 1280  # max pixels on longest side for VLM input


# ─── Helper: compress image for VLM ──────────────────────────────────


def _build_atlas_know_id(page_num: int, image_bytes: bytes) -> str:
    """Keep atlas chunk IDs deterministic while preserving per-page uniqueness."""
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    return gen_str_codes(f"{page_num}:{image_hash}")


def _build_unique_image_name(
    safe_title: str,
    img_ext: str,
    used_image_names: set[str],
) -> str:
    """Allocate a stable unique filename before any later chunk-path deduping."""
    candidate = f"{safe_title}{img_ext}"
    if candidate not in used_image_names:
        used_image_names.add(candidate)
        return candidate

    suffix = 2
    while True:
        candidate = f"{safe_title}_{suffix}{img_ext}"
        if candidate not in used_image_names:
            used_image_names.add(candidate)
            return candidate
        suffix += 1


def _compress_for_vlm(img_path: str, max_side: int = IMG_MAX_SIDE) -> str:
    """
    Resize image so the longest side <= max_side, save as JPEG for smaller size.
    Returns path to the compressed image (or original if small enough).
    """
    from PIL import Image

    img = Image.open(img_path)
    w, h = img.size

    if max(w, h) <= max_side:
        return img_path

    ratio = max_side / max(w, h)
    new_w, new_h = int(w * ratio), int(h * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    compressed_path = img_path.replace(".png", "_vlm.jpg")
    img.save(compressed_path, "JPEG", quality=85)
    return compressed_path


# ─── Helper: derive chunk title from page text ───────────────────────


def _make_title_from_text(page_text: str, page_num: int, max_len: int = 25) -> str:
    """
    Build a chunk title from the first and last non-empty lines + page number.

    Example: "P5 General entrance requirements...Scale 1:50"
    """
    lines = [line.strip() for line in page_text.split("\n") if line.strip()]
    if not lines:
        return f"P{page_num}"

    first = lines[0][:max_len]
    if len(lines) > 1:
        last = lines[-1][:max_len]
        return f"P{page_num} {first}...{last}"
    return f"P{page_num} {first}"


# ─── Helper: VLM page info extraction ────────────────────────────────


def _vlm_extract_page_info(output_dir: str, img_name: str) -> str:
    """
    Send the full page image (compressed) to VLM with atlas-page-info prompt.
    Uses IMAGE_MODEL (PLUS) for cost efficiency.

    Returns extracted info string, or empty string on failure.
    """
    from app.services.document_parser.formats.image.parser import _get_vision_client, ask_image

    img_path = os.path.join(output_dir, "images", img_name)
    if not os.path.exists(img_path):
        return ""

    # Compress for VLM
    vlm_path = _compress_for_vlm(img_path)
    # Use relative path from output_dir for ask_image
    vlm_rel = os.path.relpath(vlm_path, output_dir)

    try:
        client = _get_vision_client()
        result = ask_image(
            client,
            output_dir,
            [vlm_rel],
            title_text="",
            task="atlas-page-info",
            size_cut=False,
        )

        # Clean up compressed file if different from original
        if vlm_path != img_path:
            try:
                os.remove(vlm_path)
            except OSError as exc:
                logger.debug(f"Failed to remove compressed atlas image {vlm_path}: {exc}")

        if result:
            return result.strip()
        return ""
    except Exception as e:
        logger.warning(f"VLM atlas page info extraction failed for {img_name}: {e}")
        return ""


# ─── Child-process workers (top-level for pickling) ─────────────────


@worker
def _atlas_extract_texts_worker(queue, pdf_path):
    """Child: extract text from all pages for TOC detection + total page count."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    page_texts = []
    for page_idx in range(total_pages):
        page = doc[page_idx]
        text = page.get_text("text").strip()
        page_texts.append(text)
    doc.close()
    queue.put({"ok": True, "total_pages": total_pages, "page_texts": page_texts})


@worker
def _atlas_render_pages_worker(
    queue, pdf_path, img_dir, skip_pages_list, dpi, page_texts_list
):
    """Child: render non-skipped pages to images, reuse pre-extracted text."""
    import pymupdf

    skip_pages = set(skip_pages_list)
    doc = pymupdf.open(pdf_path)
    page_data = []

    for page_idx in range(len(doc)):
        page_num = page_idx + 1
        if page_num in skip_pages:
            continue
        page = doc[page_idx]
        page_text = page_texts_list[page_idx]
        pix = page.get_pixmap(dpi=dpi)
        img_name = f"page-{page_num}.jpg"
        # Save as JPEG instead of PNG: ~5-10x smaller for opaque page renders.
        # Atlas pages are photos/drawings with no transparency, so lossless PNG
        # provides no visual benefit while inflating images/ from ~100MB to ~10MB.
        img_bytes = pix.tobytes("jpeg", jpg_quality=85)
        with open(os.path.join(img_dir, img_name), "wb") as f:
            f.write(img_bytes)
        page_data.append((page_num, page_text, img_name))

    doc.close()
    queue.put({"ok": True, "page_data": page_data})


def _detect_toc_pages_from_texts(
    page_texts: list[str],
    model_name: str,
    hierarchy_model_name: str | None = None,
) -> tuple:
    """Detect TOC pages from pre-extracted page texts (no PyMuPDF needed).

    Args:
        page_texts: list of text strings, one per page (0-indexed).
        model_name: LLM model for TOC range detection.
        hierarchy_model_name: Optional dedicated model for TOC hierarchy parsing.

    Returns:
        (toc_page_set, toc_hierarchies)
    """
    md_lines = []
    for page_idx, text in enumerate(page_texts):
        if text:
            md_lines.append(f"<!-- page {page_idx + 1} -->")
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped:
                    md_lines.append(stripped)

    if not md_lines:
        return set(), None

    toc_hierarchies, _ = detect_tocs_in_texts(
        md_lines,
        model_name=model_name,
        hierarchy_model_name=hierarchy_model_name,
    )

    if not toc_hierarchies:
        return set(), None

    page_marker_re = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.IGNORECASE)
    line_to_page = {}
    current_page = 0
    for i, line in enumerate(md_lines):
        m = page_marker_re.search(line)
        if m:
            current_page = int(m.group(1))
        line_to_page[i] = current_page

    toc_pages = set()
    for toc_info in toc_hierarchies:
        toc_start = toc_info.get("toc_range", (0, 0))[0]
        toc_end = toc_info.get("toc_range", (0, 0))[1]
        for line_idx in range(toc_start, toc_end + 1):
            pg = line_to_page.get(line_idx, 0)
            if pg > 0:
                toc_pages.add(pg)

    return toc_pages, toc_hierarchies


# ─── Main entry point ────────────────────────────────────────────────


def parse_atlas(
    pdf_path: str,
    output_dir: str,
    base_llm_paras: dict,
    relative_root: str = None,
    profile=None,
) -> pd.DataFrame:
    """
    Atlas-specific parsing: one chunk per page using PyMuPDF directly.

    For scanned atlases (no extractable text), automatically calls VLM
    to extract drawing info from each page with 3-way concurrency.

    Args:
        pdf_path: original PDF file path
        output_dir: output directory for images, full.md, etc.
        base_llm_paras: LLM parameters dict
        relative_root: path prefix for chunk path field
        profile: parser profile with is_scanned info

    Returns:
        pd.DataFrame with ALL_DF_COLS columns
    """
    logger.info(f"📐 Atlas pipeline: starting per-page chunking for {pdf_path}")

    os.makedirs(output_dir, exist_ok=True)
    img_dir = os.path.join(output_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    # ── Phase 0: Extract all page texts in child process (fast, subsecond) ──
    text_result = run_in_child_process(
        _atlas_extract_texts_worker,
        pdf_path,
        timeout=120,
    )
    total_pages = text_result["total_pages"]
    page_texts = text_result["page_texts"]
    logger.info(f"📐 Atlas: {total_pages} pages in PDF")

    # ── Determine if VLM is needed ──
    use_vlm = True
    is_scanned = bool(profile and profile.is_scanned)
    scan_label = "scanned" if is_scanned else "non-scanned"
    logger.info(f"📐 Atlas: {scan_label} document, VLM enabled for info extraction")

    # ── TOC detection (runs in parent — uses LLM, no PyMuPDF) ──
    model_name = base_llm_paras.get("model_name", settings.NORMOL_MODEL)
    hierarchy_model_name = base_llm_paras.get("hierarchy_model_name") or model_name
    toc_page_set, toc_hierarchies = _detect_toc_pages_from_texts(
        page_texts,
        model_name,
        hierarchy_model_name=hierarchy_model_name,
    )

    if toc_page_set:
        logger.info(f"📐 Atlas: TOC pages detected: {sorted(toc_page_set)}")

    if toc_hierarchies:
        toc_json_path = os.path.join(output_dir, "toc_hierarchies.json")
        with open(toc_json_path, "w", encoding="utf-8") as f:
            json.dump(toc_hierarchies, f, ensure_ascii=False, indent=2)

    # ── Config ──
    stopwords = base_llm_paras.get("stopwords", [])
    time_stamp = get_str_time()
    split_char = settings.SPLIT_CHAR or "/"

    # ── Phase 1: Render non-TOC pages in child process (heavy, 73s for 475 pages) ──
    render_result = run_in_child_process(
        _atlas_render_pages_worker,
        pdf_path,
        img_dir,
        list(toc_page_set),
        IMG_RENDER_DPI,
        page_texts,
        timeout=600,
    )
    page_data = render_result["page_data"]
    logger.info(f"📐 Atlas: rendered {len(page_data)} pages as images")

    # ── Phase 2: VLM extraction (concurrent if needed) ──
    vlm_results = {}  # page_num → vlm_info string

    if use_vlm and page_data:
        logger.info(
            f"📐 Atlas: starting VLM extraction with {VLM_CONCURRENCY}-way concurrency"
        )

        def _vlm_task(page_num, img_name):
            info = _vlm_extract_page_info(output_dir, img_name)
            return page_num, info

        with ThreadPoolExecutor(max_workers=VLM_CONCURRENCY) as executor:
            futures = {
                executor.submit(_vlm_task, pn, iname): pn for pn, _, iname in page_data
            }
            done_count = 0
            for future in as_completed(futures):
                page_num, info = future.result()
                vlm_results[page_num] = info
                done_count += 1
                if done_count % 10 == 0:
                    logger.info(f"📐 VLM progress: {done_count}/{len(page_data)}")

        vlm_ok = sum(1 for v in vlm_results.values() if v)
        logger.info(
            f"📐 VLM extraction complete: {vlm_ok}/{len(page_data)} pages got info"
        )

    # ── Phase 3: Build chunks ──
    rows_builder = ParsedRowsBuilder()
    custom_md_lines = []
    skipped_null = 0
    used_image_names: set[str] = set()

    for page_num, page_text, img_name in page_data:
        # Determine chunk title
        vlm_info = vlm_results.get(page_num, "")

        # If VLM is active and returned null → skip this page entirely
        if use_vlm and not vlm_info:
            skipped_null += 1
            logger.debug(f"📐 Skipping page {page_num}: VLM returned null")
            continue

        if vlm_info:
            chunk_title = vlm_info[:80]
        else:
            chunk_title = _make_title_from_text(page_text, page_num)

        # Sanitize title for path usage
        safe_title = re.sub(r'[/\\:*?"<>|]', "_", chunk_title)

        # Rename image file to use descriptive title (match Phase 1 JPEG format)
        img_ext = os.path.splitext(img_name)[1]  # .jpg from Phase 1
        new_img_name = _build_unique_image_name(safe_title, img_ext, used_image_names)
        old_img_path = os.path.join(img_dir, img_name)
        new_img_path = os.path.join(img_dir, new_img_name)
        if os.path.exists(old_img_path):
            if old_img_path != new_img_path:
                os.rename(old_img_path, new_img_path)
            img_name = new_img_name

        # Build chunk content
        img_ref = f"images/{img_name}"
        # Deterministic know_id: include page identity so identical renders
        # from different atlas pages cannot collapse into the same chunk ID.
        # Read the image file saved in Phase 1 (may have been renamed above)
        actual_img_path = new_img_path if os.path.exists(new_img_path) else old_img_path
        with open(actual_img_path, "rb") as img_f:
            know_id = _build_atlas_know_id(page_num, img_f.read())
        img_marker = build_chunk_ref(img_ref)

        content_parts = [f"\n{img_marker}"]
        if vlm_info:
            content_parts.append(f"[page-{page_num}] {vlm_info}")
        else:
            content_parts.append(f"[page-{page_num}] {chunk_title}")
        if page_text:
            content_parts.append(page_text)
        content = "\n".join(content_parts) + "\n"

        # Build path
        if relative_root:
            chunk_path = f"{relative_root}{split_char}{safe_title}"
        else:
            chunk_path = safe_title

        # Atlas chunks are image-primary: use IMAGE marker directly.
        # find_matches_parsing() prepends "PTXT\n" which causes downstream
        # chunk type classifier to misclassify as "text" instead of "image".
        tokens = tokenize2stw_remove([content], stopwords)
        rows_builder.append(
            ParsedRow(
                content=content,
                path=chunk_path,
                type="image",
                know_id=know_id,
                addtime=time_stamp,
                tokens=tokens,
            )
        )

        # Build custom md line
        custom_md_lines.append(f"<!-- page {page_num} -->")
        custom_md_lines.append(f"## {chunk_title}")
        custom_md_lines.append(f"![{chunk_title}]({img_ref})")
        if vlm_info:
            custom_md_lines.append(vlm_info)
        elif page_text:
            custom_md_lines.append(page_text[:200])
        custom_md_lines.append("")

    # ── Save custom full.md ──
    custom_md_path = os.path.join(output_dir, "full.md")
    with open(custom_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(custom_md_lines))

    df = rows_builder.to_dataframe()
    logger.info(
        f"📐 Atlas pipeline complete: {len(df)} chunks created "
        f"(skipped {len(toc_page_set)} TOC + {skipped_null} null pages, total {total_pages})"
    )

    # TODO: build atlas hierarchy structure (e.g. atlas_name → page chunks)
    #       Currently atlas chunks are flat with no parent-child relationships.
    #       Future: integrate with hierarchy builder for unified schema.

    df = process_dup_paths_df(df)
    return df
