"""Legacy VML graphics in a DOCX body.

Older Word files draw pictures with VML (``<w:pict>``) instead of DrawingML.
A single VML picture points at one media file, which is read as is. A VML
group stacks pictures, shapes and text boxes into one graphic; taking any one
part loses the overlay, so the whole group is rendered as one image.
"""

from __future__ import annotations

import gc
import io
import os
import tempfile
import zipfile

from loguru import logger
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from app.services.document_parser.conversion.legacy_converter import (
    _convert_with_libreoffice,
)
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from shared.core.constants.processing import ProcessingConstants

_DOCUMENT_PART = "word/document.xml"
_COMPOSITE_PDF_NAME = "vml-composites"


def vml_pictures(element, ns: dict[str, str]) -> list:
    """Outermost ``<w:pict>`` graphics under ``element``.

    ``mc:Fallback`` repeats a DrawingML picture as VML for old readers, and
    the DrawingML copy is already read. A picture inside another one (in a
    text box of a group) is drawn with that group.
    """
    return element.xpath(
        ".//w:pict[not(ancestor::mc:Fallback)][not(ancestor::w:pict)]", namespaces=ns
    )


def is_composite(pict, ns: dict[str, str]) -> bool:
    return bool(pict.xpath(".//v:group", namespaces=ns))


def single_picture_rid(pict, ns: dict[str, str]) -> str | None:
    """Relationship id of a VML picture that holds exactly one image."""
    if is_composite(pict, ns):
        return None
    rids = pict.xpath(".//v:imagedata/@r:id", namespaces=ns)
    return str(rids[0]) if len(rids) == 1 else None


def composite_image_name(index: int) -> str:
    return f"vml-composite-{index}.png"


def render_composites(
    package_bytes: bytes, root, composites: list, ns: dict[str, str]
) -> dict[str, tuple[str, bytes]]:
    """Image name and PNG bytes of each composite, keyed by its path in ``root``.

    The composites are copied, one per page, into a work copy of the package
    without headers, footers or page background, rendered once by
    LibreOffice, and each page is cropped to what it draws.
    """
    if not composites:
        return {}
    tree = root.getroottree()
    paths = [tree.getpath(pict) for pict in composites]
    with tempfile.TemporaryDirectory(prefix="docx-vml-") as work_dir:
        source_path = os.path.join(work_dir, f"{_COMPOSITE_PDF_NAME}.docx")
        _write_composite_package(package_bytes, root, composites, ns, source_path)
        try:
            pdf_path, _pdf_name = _convert_with_libreoffice(
                source_path=source_path,
                outdir=work_dir,
                convert_to_arg="pdf:writer_pdf_Export",
                expected_output_ext="pdf",
                operation="render_docx_vml_composites",
            )
            result = run_in_child_process(
                _render_cropped_pages_worker,
                pdf_path,
                ProcessingConstants.PAGE_RENDER_DPI,
            )
        except Exception as exc:
            logger.warning(f"VML composite rendering failed; keeping placeholders: {exc}")
            return {}
    images: list[bytes | None] = list(result.get("images") or [])
    if len(images) != len(composites):
        # A composite taller than a page spills onto the next one, so pages no
        # longer line up with composites.
        logger.warning(
            f"VML composite rendering gave {len(images)} pages for "
            f"{len(composites)} composites; keeping placeholders"
        )
        return {}
    return {
        path: (composite_image_name(index), image)
        for index, (path, image) in enumerate(zip(paths, images), start=1)
        if image
    }


def _write_composite_package(
    package_bytes: bytes, root, composites: list, ns: dict[str, str], destination: str
) -> None:
    w = ns["w"]
    document = etree.fromstring(etree.tostring(root))
    body = document.find("w:body", ns)
    section = body.find("w:sectPr", ns)
    for child in list(body):
        body.remove(child)
    for index, pict in enumerate(composites):
        if index:
            page_break = etree.SubElement(
                etree.SubElement(etree.SubElement(body, f"{{{w}}}p"), f"{{{w}}}r"),
                f"{{{w}}}br",
            )
            page_break.set(f"{{{w}}}type", "page")
        run = etree.SubElement(etree.SubElement(body, f"{{{w}}}p"), f"{{{w}}}r")
        run.append(etree.fromstring(etree.tostring(pict)))
    if section is not None:
        for reference in section.xpath(
            "w:headerReference | w:footerReference", namespaces=ns
        ):
            section.remove(reference)
        body.append(section)
    for background in document.findall("w:background", ns):
        document.remove(background)
    document_xml = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as source:
            for item in source.infolist():
                data = (
                    document_xml
                    if item.filename == _DOCUMENT_PART
                    else source.read(item.filename)
                )
                target.writestr(item, data)


@worker
def _render_cropped_pages_worker(queue, pdf_path: str, dpi: int) -> None:
    import pymupdf  # type: ignore[import]

    images: list[bytes | None] = []
    document = pymupdf.open(pdf_path)
    try:
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        for page in document:
            drawn = [
                pymupdf.Rect(rect)
                for _kind, rect in page.get_bboxlog()
                if not pymupdf.Rect(rect).is_empty
            ]
            if not drawn:
                images.append(None)
                continue
            area = drawn[0]
            for rect in drawn[1:]:
                area |= rect
            images.append(page.get_pixmap(matrix=matrix, clip=area).tobytes("png"))
    finally:
        document.close()
        gc.collect()
    queue.put({"ok": True, "images": images})
