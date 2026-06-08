"""
main parsing service
"""
import os
import re

from shared.core.config import settings
from shared.utils.file_utils import path_handle
from loguru import logger
from shared.core.exceptions.domain_exceptions import (
    ValidationException,
)

# document_parser imports
from app.services.document_parser.doc_profiler import DocProfile, profile_document
from app.services.document_parser.stage_profiler import stage_timer
from app.services.document_parser.atlas_classifier import classify_atlas_with_vlm

PDF_PAGE_LIMIT = 600


def _count_pdf_pages(file_full_path: str) -> int:
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_full_path)
        return max(1, len(reader.pages))
    except Exception as err:
        logger.warning(f"Failed to count forced atlas PDF pages for {file_full_path}: {err}")
        return 0


def _create_forced_atlas_profile(file_full_path: str, page_count: int | None) -> DocProfile:
    """Create the routing profile for an explicit client atlas override."""
    forced_page_count = page_count if page_count is not None else _count_pdf_pages(file_full_path)
    return DocProfile(
        file_type="pdf",
        route="standard",
        decision_band="safe_standard",
        doc_category="atlas",
        page_count=forced_page_count,
        reasoning="forced_atlas=True",
    )


def _enforce_pdf_page_limit(profile: DocProfile | None) -> None:
    if profile and profile.file_type == "pdf" and profile.page_count > PDF_PAGE_LIMIT:
        raise ValidationException(
            user_message=(
                f"Document too large: {profile.page_count} pages exceeds the {PDF_PAGE_LIMIT}-page limit. "
                f"Please split the document and upload in smaller batches."
            ),
            violations=[{
                "field": "page_count",
                "description": f"PDF has {profile.page_count} pages, limit is {PDF_PAGE_LIMIT}",
            }],
        )


def cleanup_unreferenced_images(output_dir: str) -> int:
    """
    Clean up unreferenced UUID-named images from the images directory.
    
    After document parsing (PDF, DOCX, PPTX, etc.), the images/ directory may contain:
    1. Processed images: renamed with semantic names like 'image-0-xxx.jpg'
    2. Unreferenced images: UUID-named (64-char hex) that were parsed as tables/formulas
    
    This function removes the unreferenced UUID-named images to reduce final package size.
    
    Args:
        output_dir: The full output directory path
    
    Returns:
        Number of files removed
    """
    img_dir = os.path.join(output_dir, "images")
    if not os.path.isdir(img_dir):
        return 0
    
    # UUID pattern: 64 hex characters followed by image extension
    uuid_pattern = re.compile(r'^[a-f0-9]{64}\.(?:jpg|jpeg|png|gif|webp)$', re.IGNORECASE)
    
    removed_count = 0
    for filename in os.listdir(img_dir):
        if uuid_pattern.match(filename):
            file_path = os.path.join(img_dir, filename)
            try:
                os.remove(file_path)
                removed_count += 1
                logger.debug(f"Removed unreferenced image: {filename}")
            except OSError as e:
                logger.warning(f"Failed to remove {filename}: {e}")
    
    if removed_count > 0:
        logger.info(f"Cleaned up {removed_count} unreferenced UUID-named images from {img_dir}")
    
    return removed_count

def checkerboard_inject_parse(
    file_full_path: str, 
    filename: str, 
    output_dir: str,
    internal_output_filename: str,
    job_id: str | None = None,
    kb_dir: str = "Default_Root",
    llm_histories: int = 5,
    smart_title_parse: bool = True,
    summary_image: bool = True,
    summary_table: bool = True,
    summary_txt: bool = True,
    stopwords: list[str] | None = None,
    doc_type: str = "auto",
    add_frag_desc: str = "",
    base_url: str = "",
    fragment_content: str = "",
    s3_key: str | None = None,
    is_atlas: bool = False,
    page_count: int | None = None,
) -> tuple[str, object]:
    """
    main parsing function
    
    Args:
        file_full_path: source file path (local or URL)
        filename: file name
        output_dir: output directory (absolute path, caller provides)
        kb_dir: sub-directory name
        llm_histories: retained for downstream LLM settings
        smart_title_parse: enable smart heading parsing
        summary_image: enable image summaries
        summary_table: enable table summaries
        summary_txt: enable text summaries
        stopwords: optional stopword list
        doc_type: parser document type hint
        add_frag_desc: extra fragment description
        base_url: optional source base URL
        fragment_content: raw fragment content
        job_id: optional job identifier used for parser artifacts
        internal_output_filename: normalized internal folder name for on-disk output
        s3_key: optional S3 key for downstream parsers
        is_atlas: force PDF input through the atlas parser when true
        page_count: optional precomputed page count used for forced atlas guards
    
    Returns:
        tuple: (output_dir, parsed_df)
            - output_dir: directory path after parsing
            - parsed_df: parsed content DataFrame
    """
    # Build base_llm_paras from explicit parameters
    base_llm_paras = {
        "llm_histories": llm_histories,
        "smart_title_parse": smart_title_parse,
        "summary_image": summary_image,
        "summary_table": summary_table,
        "summary_txt": summary_txt,
        "stopwords": stopwords,
        "doc_type": doc_type,
        "frag_desc": add_frag_desc,
        "model_name": settings.NORMOL_MODEL,
    }
    
    baseurl = base_url
        
    logger.debug(f"baseurl: {baseurl}")
    logger.debug(f"file_full_path: {file_full_path}")
    
    # ========== Path handling ==========
    split_char = settings.SPLIT_CHAR or "/"
    
    # Develop relative root path for chunk path field
    kb_dir_parts = kb_dir.split(split_char)
    if filename and "images" not in kb_dir_parts:
        relative_root = "/".join(kb_dir_parts + [filename])
    else:
        relative_root = "/".join(kb_dir_parts)

    if internal_output_filename and "images" not in kb_dir_parts:
        internal_relative_root = "/".join(kb_dir_parts + [internal_output_filename])
    else:
        internal_relative_root = "/".join(kb_dir_parts)
    
    # Develop full output directory (output_dir + relative_root)
    full_output_dir = os.path.join(output_dir, internal_relative_root.replace("/", os.sep))
    full_output_dir = path_handle(full_output_dir, mode="sanitize")
    os.makedirs(full_output_dir, exist_ok=True)
    
    logger.debug(f"relative_root: {relative_root}")
    logger.debug(f"internal_relative_root: {internal_relative_root}")
    logger.debug(f"full_output_dir: {full_output_dir}")

    file_path_lower = file_full_path.lower()
    parsed_df = None
    should_force_atlas = is_atlas and ".pdf" in file_path_lower
    
    # ── Agentic Profiler: classify document before routing ──
    if should_force_atlas:
        profile = _create_forced_atlas_profile(file_full_path, page_count)
        logger.info(f"📐 Forced atlas parsing enabled for {filename}")
    else:
        with stage_timer("document.profile", filename=filename):
            profile = profile_document(file_full_path, internal_output_filename)
    logger.info(f"📋 DocProfile: {profile.summary()}")
    logger.debug(f"📋 Reasoning: {profile.reasoning}")

    # ── VLM second-pass: confirm atlas_candidate with visual check ──
    # Heuristics can miss atlases that have a rich OCR text layer on top of
    # scanned drawing pages (avg_text_density too high). VLM sees the actual
    # page layout and makes the final call.
    if not should_force_atlas and profile.atlas_candidate and profile.doc_category not in ("atlas", "ppt_converted"):
        logger.info(f"🔍 Atlas candidate detected, running VLM visual check for {filename}")
        with stage_timer("document.atlas_vlm_check", filename=filename):
            vlm_is_atlas = classify_atlas_with_vlm(file_full_path)
        if vlm_is_atlas:
            profile.doc_category = "atlas"
            profile.reasoning += " | vlm_confirmed_atlas=True"
            logger.info(f"✅ VLM confirmed atlas for {filename}")
        else:
            profile.reasoning += " | vlm_confirmed_atlas=False"
            logger.info(f"ℹ️ VLM rejected atlas for {filename}, routing as generic")

    # ── Page count guard: reject oversized PDFs before routing ──
    _enforce_pdf_page_limit(profile)

    # Atlas routing: rename output folder from .pdf → .atlas for easy filtering
    if profile and profile.doc_category == "atlas":
        name_base, _ = os.path.splitext(filename)
        internal_name_base, _ = os.path.splitext(internal_output_filename)
        filename = name_base + ".atlas"
        internal_output_filename = internal_name_base + ".atlas"
        relative_root = "/".join(kb_dir_parts + [filename])
        internal_relative_root = "/".join(kb_dir_parts + [internal_output_filename])
        full_output_dir = os.path.join(output_dir, internal_relative_root.replace("/", os.sep))
        full_output_dir = path_handle(full_output_dir, mode="sanitize")
        os.makedirs(full_output_dir, exist_ok=True)
        logger.info(f"📐 Atlas output renamed: {filename}")
    
    if ".fragment" in file_path_lower:
        logger.debug("file type is fragment")
        from app.services.document_parser.fragment_parser import parse_fragment

        full_output_dir, relative_root, parsed_df = parse_fragment(fragment_content, filename=filename, output_dir=output_dir, kb_dir=kb_dir, base_llm_paras=base_llm_paras)

    elif '.txt' in file_path_lower:
        logger.debug("file type is txt")
        from app.services.document_parser.md_parser import parse_md
        from app.services.document_parser.txt_parser import parse_texts

        txt_lines = parse_texts(file_path=file_full_path, baseurl=baseurl)
        parsed_df = parse_md(full_output_dir, source_type='md', md_lines=txt_lines, base_llm_paras=base_llm_paras, relative_root=relative_root)

    elif ('.png' in file_path_lower or '.jpg' in file_path_lower or '.jpeg' in file_path_lower):
        logger.debug(f"file type is image")
        from app.services.document_parser.image_parser import parse_image

        parsed_df = parse_image(file_full_path, filename=filename, output_dir=full_output_dir, baseurl=baseurl, base_llm_paras=base_llm_paras, relative_root=relative_root)

    elif '.pdf' in file_path_lower:
        logger.debug(f"file type is pdf")
        from app.services.document_parser.pdf_parser import parse_pdfs

        if filename and file_full_path:
            parsed_df = parse_pdfs(file_full_path, filename=filename, output_dir=full_output_dir, base_llm_paras=base_llm_paras, profile=profile, relative_root=relative_root, s3_key=s3_key)

    elif '.doc' in file_path_lower and '.docx' not in file_path_lower:
        logger.debug("file type is doc")
        from app.services.document_parser.doc_parser import convert_doc2dics, parse_docx
        from app.services.document_parser.legacy_converter import doc_to_docx

        if filename and file_full_path:
            converted_docx_path, _ = doc_to_docx(file_full_path, outdir=full_output_dir)
            parsed_structure, df_list = parse_docx(
                converted_docx_path,
                base_llm_paras,
                full_output_dir,
                filename,
                baseurl,
                relative_root=relative_root,
            )
            parsed_df = convert_doc2dics(
                parsed_structure,
                df_list,
                full_output_dir,
                base_llm_paras=base_llm_paras,
                relative_root=relative_root,
            )

    elif '.docx' in file_path_lower:
        logger.debug(f"file type is docx")
        from app.services.document_parser.doc_parser import convert_doc2dics, parse_docx

        if filename and file_full_path:
            parsed_structure, df_list = parse_docx(file_full_path, base_llm_paras, full_output_dir, filename, baseurl, relative_root=relative_root)
            parsed_df = convert_doc2dics(parsed_structure, df_list, full_output_dir, base_llm_paras=base_llm_paras, relative_root=relative_root)

    elif '.xls' in file_path_lower and '.xlsx' not in file_path_lower:
        logger.debug("file type is xls")
        from app.services.document_parser.legacy_converter import xls_to_xlsx
        from app.services.document_parser.table_parser import parse_xlsx

        if filename and file_full_path:
            converted_xlsx_path, _ = xls_to_xlsx(file_full_path, outdir=full_output_dir)
            parsed_df = parse_xlsx(
                converted_xlsx_path,
                filename,
                full_output_dir,
                baseurl,
                base_llm_paras=base_llm_paras,
                relative_root=relative_root,
            )

    elif '.xlsx' in file_path_lower:
        logger.debug(f"file type is xlsx")
        from app.services.document_parser.table_parser import parse_xlsx

        if filename and file_full_path:
            parsed_df = parse_xlsx(file_full_path, filename, full_output_dir, baseurl, base_llm_paras=base_llm_paras, relative_root=relative_root)

    elif '.pptx' in file_path_lower:
        logger.debug(f"file type is pptx")
        from app.services.document_parser.pptx_parser import parse_pptx

        if filename and file_full_path:
            # ====== iLoveAPI PPTX → PDF → MinerU (default production route) ======
            parsed_df = parse_pptx(
                file_full_path, filename=filename,
                output_dir=full_output_dir,
                base_llm_paras=base_llm_paras,
                strategy="to_pdf_api",
                job_id=job_id,
                relative_root=relative_root,
                baseurl=baseurl,
            )
            
            # ====== [EXPERIMENTAL] Directly send PPTX to MinerU via parse_pdfs ======
            # Uncomment the block below (and comment out parse_pptx above) to bypass iLoveAPI
            # from app.services.document_parser.pdf_parser import parse_pdfs
            # parsed_df = parse_pdfs(
            #     file_full_path,
            #     filename=filename,
            #     output_dir=full_output_dir,
            #     base_llm_paras=base_llm_paras,
            #     profile=profile,
            #     relative_root=relative_root,
            #     s3_key=s3_key
            # )

    elif '.md' in file_path_lower:
        logger.debug(f"file type is md")
        from app.services.document_parser.md_parser import parse_md

        if filename and file_full_path:
            parsed_df = parse_md(full_output_dir, source_type="md", file_path=file_full_path, base_llm_paras=base_llm_paras, relative_root=relative_root)

    elif '.json' in file_path_lower:
        logger.debug(f"file type is json")
        # JSON parsing not yet implemented
        
    else:
        # Unsupported file type
        file_ext = os.path.splitext(file_full_path)[1].lower()
        supported_types = ['.txt', '.fragment', '.png', '.jpg', '.jpeg', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.pptx', '.md', '.json']
        raise ValidationException(
            user_message=f"Unsupported file type: {file_ext}",
            violations=[{
                "field": "file_type",
                "description": f"Must be one of: {', '.join(supported_types)}"
            }]
        )
        
    logger.debug(f"full_output_dir: {full_output_dir}")
    
    # Post-processing: clean up unreferenced UUID-named images
    with stage_timer("document.cleanup_unreferenced_images", output_dir=full_output_dir):
        cleanup_unreferenced_images(full_output_dir)

    # Post-processing: compress output images (PNG→JPEG, resize oversized)
    from app.services.document_parser.image_compressor import (
        compress_output_images,
        apply_rename_map_to_dataframe,
    )
    with stage_timer("document.compress_images", output_dir=full_output_dir):
        compress_stats = compress_output_images(full_output_dir)
        if compress_stats.processed > 0:
            logger.info(
                f"📦 Image compression: {compress_stats.processed} processed "
                f"({compress_stats.converted_png_to_jpg} PNG→JPG, "
                f"{compress_stats.resized} resized), "
                f"{compress_stats.bytes_before / 1024 / 1024:.1f}MB → "
                f"{compress_stats.bytes_after / 1024 / 1024:.1f}MB"
            )
        # Update DataFrame references when PNG→JPG conversions occurred
        if compress_stats.rename_map and parsed_df is not None:
            parsed_df = apply_rename_map_to_dataframe(parsed_df, compress_stats.rename_map)

    return full_output_dir, parsed_df
