"""Legacy graphics in a DOCX body: VML pictures and embedded objects.

Older Word files draw pictures with VML (``<w:pict>``) instead of DrawingML.
A single VML picture points at one media file, which is read as is. Two kinds
of graphic have no file to read and are rendered as images instead:

- a VML group stacks pictures, shapes and text boxes into one graphic; taking
  any one part loses the overlay, so the whole group is rendered;
- an embedded object (``<w:object>``: Visio, Photoshop, Paint, AutoCAD, ...)
  only keeps a WMF/EMF preview, which is rendered.

An embedded equation (Equation Editor 3.0 or MathType) is not rendered; the
paragraph text marks where it stands.
"""

from __future__ import annotations

import gc
import io
import os
import re
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
_RENDER_PACKAGE_NAME = "rendered-graphics"
_OFFICE_NS = "urn:schemas-microsoft-com:office:office"
_EQUATION_PROG_ID_PREFIX = "Equation."
# Text of the page put before each graphic, followed by its number, so the
# rendered pages can be matched to graphics however many pages each takes.
_RENDER_MARK_PREFIX = "knowhere-render-mark-"
_UNRENDERED_LABEL = "graphic not extracted"


def vml_pictures(element, ns: dict[str, str]) -> list:
    """Outermost ``<w:pict>`` graphics under ``element``.

    ``mc:Fallback`` repeats a DrawingML picture as VML for old readers, and
    the DrawingML copy is already read. A picture inside another one (in a
    text box of a group) is drawn with that group.
    """
    return element.xpath(
        ".//w:pict[not(ancestor::mc:Fallback)][not(ancestor::w:pict)]", namespaces=ns
    )


def is_vml_group(pict, ns: dict[str, str]) -> bool:
    return bool(pict.xpath(".//v:group", namespaces=ns))


def single_picture_rid(pict, ns: dict[str, str]) -> str | None:
    """Relationship id of a VML picture that holds exactly one image."""
    if is_vml_group(pict, ns):
        return None
    rids = pict.xpath(".//v:imagedata/@r:id", namespaces=ns)
    return str(rids[0]) if len(rids) == 1 else None


def object_prog_id(obj) -> str | None:
    """The program an embedded object was made with, as Word records it."""
    for ole in obj.iter(f"{{{_OFFICE_NS}}}OLEObject"):
        return ole.get("ProgID")
    return None


def is_equation_object(obj) -> bool:
    prog_id = object_prog_id(obj)
    return prog_id is not None and prog_id.startswith(_EQUATION_PROG_ID_PREFIX)


def rendered_graphics(element, ns: dict[str, str]) -> list:
    """VML groups and non-equation embedded objects under ``element``, in order."""
    candidates = element.xpath(
        ".//w:pict[not(ancestor::mc:Fallback)][not(ancestor::w:pict)]"
        " | .//w:object[not(ancestor::mc:Fallback)][not(ancestor::w:pict)]",
        namespaces=ns,
    )
    return [
        graphic
        for graphic in candidates
        if (
            is_vml_group(graphic, ns)
            if etree.QName(graphic).localname == "pict"
            else not is_equation_object(graphic)
        )
    ]


def unrendered_placeholder(graphic) -> str:
    """Text left where a graphic could not be rendered, naming an object's program."""
    if etree.QName(graphic).localname == "object":
        prog_id = object_prog_id(graphic)
        if prog_id:
            return f"[{_UNRENDERED_LABEL}: {prog_id}]"
    return f"[{_UNRENDERED_LABEL}]"


def rendered_image_name(graphic_number: int, part_number: int | None = None) -> str:
    if part_number is None:
        return f"rendered-graphic-{graphic_number}.png"
    return f"rendered-graphic-{graphic_number}-{part_number}.png"


def render_graphics(
    package_bytes: bytes, root, graphics: list, ns: dict[str, str]
) -> dict[str, list[tuple[str, bytes]]]:
    """Image names and PNG bytes of each graphic, keyed by its path in ``root``.

    The graphics are copied into a work copy of the package without headers,
    footers or page background, each after a page holding only its mark, and
    rendered once by LibreOffice. The pages between one mark and the next
    belong to that graphic and are each cropped to what they draw. A graphic
    whose pages draw nothing is left out.
    """
    if not graphics:
        return {}
    tree = root.getroottree()
    with tempfile.TemporaryDirectory(prefix="docx-graphics-") as work_dir:
        source_path = os.path.join(work_dir, f"{_RENDER_PACKAGE_NAME}.docx")
        _write_render_package(package_bytes, root, graphics, ns, source_path)
        try:
            pdf_path, _pdf_name = _convert_with_libreoffice(
                source_path=source_path,
                outdir=work_dir,
                convert_to_arg="pdf:writer_pdf_Export",
                expected_output_ext="pdf",
                operation="render_docx_graphics",
            )
            result = run_in_child_process(
                _render_marked_pages_worker,
                pdf_path,
                ProcessingConstants.PAGE_RENDER_DPI,
                _RENDER_MARK_PREFIX,
            )
        except Exception as exc:
            logger.warning(f"Graphic rendering failed; keeping placeholders: {exc}")
            return {}
    pages_by_graphic = _pages_by_graphic(result["pages"], len(graphics))
    if pages_by_graphic is None:
        return {}
    rendered: dict[str, list[tuple[str, bytes]]] = {}
    for number, (graphic, pages) in enumerate(zip(graphics, pages_by_graphic), start=1):
        if not pages:
            continue
        if len(pages) == 1:
            names = [rendered_image_name(number)]
        else:
            names = [
                rendered_image_name(number, part) for part in range(1, len(pages) + 1)
            ]
        rendered[tree.getpath(graphic)] = list(zip(names, pages))
    return rendered


def _pages_by_graphic(
    pages: list[tuple[str, int | bytes | None]], graphic_count: int
) -> list[list[bytes]] | None:
    """Drawn pages of each graphic, or None when the marks are not in order."""
    pages_by_graphic: list[list[bytes]] = [[] for _ in range(graphic_count)]
    current: int | None = None
    for kind, value in pages:
        if kind == "mark":
            expected = 0 if current is None else current + 1
            if value != expected:
                logger.warning(
                    f"Graphic rendering found mark {value} where {expected} was "
                    "expected; keeping placeholders"
                )
                return None
            current = expected
        elif current is None:
            logger.warning(
                "Graphic rendering drew a page before the first mark; "
                "keeping placeholders"
            )
            return None
        elif isinstance(value, bytes):
            pages_by_graphic[current].append(value)
    found = 0 if current is None else current + 1
    if found != graphic_count:
        logger.warning(
            f"Graphic rendering found {found} marks for {graphic_count} graphics; "
            "keeping placeholders"
        )
        return None
    return pages_by_graphic


def _write_render_package(
    package_bytes: bytes, root, graphics: list, ns: dict[str, str], destination: str
) -> None:
    w = ns["w"]
    document = etree.fromstring(etree.tostring(root))
    body = document.find("w:body", ns)
    section = body.find("w:sectPr", ns)
    for child in list(body):
        body.remove(child)
    for index, graphic in enumerate(graphics):
        mark = _page_paragraph(body, w, page_break=bool(index))
        mark_text = etree.SubElement(etree.SubElement(mark, f"{{{w}}}r"), f"{{{w}}}t")
        mark_text.text = f"{_RENDER_MARK_PREFIX}{index}"
        holder = _page_paragraph(body, w, page_break=True)
        etree.SubElement(holder, f"{{{w}}}r").append(
            etree.fromstring(etree.tostring(graphic))
        )
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


def _page_paragraph(body, w: str, *, page_break: bool):
    paragraph = etree.SubElement(body, f"{{{w}}}p")
    if page_break:
        etree.SubElement(
            etree.SubElement(paragraph, f"{{{w}}}pPr"), f"{{{w}}}pageBreakBefore"
        )
    return paragraph


@worker
def _render_marked_pages_worker(
    queue, pdf_path: str, dpi: int, mark_prefix: str
) -> None:
    import pymupdf  # type: ignore[import]

    mark = re.compile(re.escape(mark_prefix) + r"(\d+)")
    pages: list[tuple[str, int | bytes | None]] = []
    document = pymupdf.open(pdf_path)
    try:
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        for page in document:
            # The "text" mode returns the page text as one string.
            marked = mark.fullmatch(str(page.get_text("text")).strip())
            if marked:
                pages.append(("mark", int(marked.group(1))))
                continue
            drawn = [
                pymupdf.Rect(rect)
                for _kind, rect in page.get_bboxlog()
                if not pymupdf.Rect(rect).is_empty
            ]
            if not drawn:
                pages.append(("page", None))
                continue
            area = drawn[0]
            for rect in drawn[1:]:
                area |= rect
            pages.append(
                ("page", page.get_pixmap(matrix=matrix, clip=area).tobytes("png"))
            )
    finally:
        document.close()
        gc.collect()
    queue.put({"ok": True, "pages": pages})
