import os
import tempfile

from app.services.document_parser.formats.pdf.pymupdf_subprocess import run_in_child_process, worker
from loguru import logger


@worker
def _render_pdf_worker(queue, src_pdf_path, dst_pdf_path, scale):
    """Child process: render PDF pages as images into a new image-only PDF."""
    import pymupdf

    src_doc = pymupdf.open(src_pdf_path)
    img_doc = pymupdf.open()
    mat = pymupdf.Matrix(scale, scale)

    for page in src_doc:
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("jpeg", jpg_quality=95)
        new_page = img_doc.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(page.rect, stream=img_bytes)

    img_doc.save(dst_pdf_path)
    page_count = len(src_doc)
    img_doc.close()
    src_doc.close()
    queue.put({"ok": True, "page_count": page_count})


def render_pdf_to_image_pdf(pdf_bytes: bytes, scale: int = 3) -> bytes:
    """
    Render each page of a PDF as a high-res image and create an image-only PDF.

    Why? iLoveAPI/LibreOffice renders math formulas as vector paths in PDF.
    MinerU cannot extract these as text (produces '????'). By converting to
    images, MinerU is forced to use its VLM model which correctly OCRs
    formulas into LaTeX.

    PyMuPDF work runs in a spawned child process for thread safety.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as src:
        src.write(pdf_bytes)
        src_path = src.name
    dst_path = src_path + ".rendered.pdf"

    try:
        result = run_in_child_process(
            _render_pdf_worker,
            src_path,
            dst_path,
            scale,
        )
        with open(dst_path, "rb") as file_obj:
            rendered = file_obj.read()
        logger.info(
            f"[parse_pptx] Image-only PDF rendered: "
            f"{len(rendered) / 1024:.1f} KB, {result['page_count']} pages"
        )
        return rendered
    finally:
        os.unlink(src_path)
        if os.path.exists(dst_path):
            os.unlink(dst_path)
