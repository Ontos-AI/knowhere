from __future__ import annotations

import io
import os
import random

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from docx import Document  # noqa: E402
from docx.oxml import OxmlElement, parse_xml  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from shared.core.constants.processing import ProcessingConstants  # noqa: E402
from shared.core.exceptions.domain_exceptions import (  # noqa: E402
    LibreOfficeServiceException,
)
from app.services.document_parser.conversion.legacy_converter import (  # noqa: E402
    resolve_libreoffice_binary,
)

_VML_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def _noise_png() -> bytes:
    """A PNG above the image size floor; random pixels do not compress."""
    side = 128
    pixels = random.Random(7).randbytes(side * side * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (side, side), pixels).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert len(data) >= ProcessingConstants.IMG_MIN_SIZE
    return data


def _docx_with_picture_in_content_control() -> bytes:
    document = Document()
    document.add_picture(io.BytesIO(_noise_png()))
    picture_paragraph = document.paragraphs[-1]._p
    sdt = OxmlElement("w:sdt")
    sdt_content = OxmlElement("w:sdtContent")
    sdt.append(sdt_content)
    picture_paragraph.addprevious(sdt)
    sdt_content.append(picture_paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _block_items(package: bytes) -> list:
    """Blocks from the live parser module.

    Contract fixtures clear ``app.*`` from ``sys.modules``; a worker function
    from a stale module cannot be pickled for the rendering child process.
    """
    from app.services.document_parser.formats.docx.block_stream import (
        iter_block_items,
    )

    return list(iter_block_items(package))


def _images(blocks) -> list[dict]:
    return [meta for _num, _content, label, meta in blocks if label == "IMAGE"]


def test_picture_inside_content_control_is_extracted() -> None:
    blocks = _block_items(_docx_with_picture_in_content_control())

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["data"].startswith(b"\x89PNG")


def _small_png() -> bytes:
    """A PNG below the image size floor."""
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert len(data) < ProcessingConstants.IMG_MIN_SIZE
    return data


def _docx_with(*run_xml: str, in_table: bool = False) -> bytes:
    """A DOCX with one paragraph per ``run_xml``, in the body or in table cells.

    ``{ns}`` is replaced by the namespaces and ``{rid}`` by the id of an image.
    """
    document = Document()
    rid, _image = document.part.get_or_add_image(io.BytesIO(_noise_png()))
    if in_table:
        cells = document.add_table(rows=1, cols=len(run_xml)).rows[0].cells
        paragraphs = [cell.paragraphs[0] for cell in cells]
    else:
        paragraphs = [document.add_paragraph() for _ in run_xml]
    for paragraph, xml in zip(paragraphs, run_xml):
        run = paragraph.add_run()
        run._r.append(parse_xml(xml.format(ns=_VML_NAMESPACES, rid=rid)))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _docx_with_paragraph_xml(paragraph_xml: str) -> bytes:
    """A DOCX whose only paragraph is ``paragraph_xml``."""
    document = Document()
    paragraph = document.add_paragraph()._p
    paragraph.addnext(parse_xml(paragraph_xml.format(ns=_VML_NAMESPACES)))
    paragraph.getparent().remove(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_SINGLE_PICTURE = (
    '<w:pict {ns}><v:shape style="width:96pt;height:96pt">'
    '<v:imagedata r:id="{rid}"/></v:shape></w:pict>'
)
_VML_GROUP = (
    '<w:pict {ns}><v:group style="width:240pt;height:120pt" '
    'coordsize="240,120">'
    '<v:shape style="position:absolute;left:0;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    '<v:shape style="position:absolute;left:130;top:40;width:110;height:40">'
    "<v:textbox><w:txbxContent><w:p><w:r><w:t>Caption</w:t></w:r></w:p>"
    "</w:txbxContent></v:textbox></v:shape>"
    "</v:group></w:pict>"
)
_TEXTLESS_VML_GROUP = (
    '<w:pict {ns}><v:group style="width:240pt;height:120pt" coordsize="240,120">'
    '<v:shape style="position:absolute;left:0;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    '<v:shape style="position:absolute;left:120;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    "</v:group></w:pict>"
)


def _embedded_object(prog_id: str) -> str:
    return (
        '<w:object {ns}><v:shape style="width:120pt;height:120pt">'
        '<v:imagedata r:id="{rid}"/></v:shape>'
        f'<o:OLEObject Type="Embed" ProgID="{prog_id}" DrawAspect="Content"/>'
        "</w:object>"
    )


_VISIO_OBJECT = _embedded_object("Visio.Drawing.11")
_EQUATION_OBJECT = _embedded_object("Equation.3")


def _texts(blocks) -> list[str]:
    from app.services.document_parser.formats.docx.paragraph_text import (
        paragraph_text,
    )
    from docx.text.paragraph import Paragraph

    return [
        paragraph_text(content) if isinstance(content, Paragraph) else content
        for _num, content, label, _meta in blocks
        if label == "PTXT"
    ]


def _libreoffice_available() -> bool:
    try:
        resolve_libreoffice_binary()
    except LibreOfficeServiceException:
        return False
    return True


requires_libreoffice = pytest.mark.skipif(
    not _libreoffice_available(), reason="LibreOffice is not installed"
)


def _fake_rendering(monkeypatch, pages) -> None:
    """Rendering that yields ``pages`` as the worker would, without LibreOffice."""
    from app.services.document_parser.formats.docx import legacy_graphics

    monkeypatch.setattr(
        legacy_graphics,
        "_convert_with_libreoffice",
        lambda **_kwargs: ("unused.pdf", "unused.pdf"),
    )
    monkeypatch.setattr(
        legacy_graphics,
        "run_in_child_process",
        lambda *_args, **_kwargs: {"ok": True, "pages": pages},
    )


def test_single_vml_picture_reads_its_media_file() -> None:
    blocks = _block_items(_docx_with(_SINGLE_PICTURE))

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["data"] == _noise_png()
    assert not any("not extracted" in text for text in _texts(blocks))


@requires_libreoffice
def test_vml_group_becomes_one_rendered_image() -> None:
    from app.services.document_parser.formats.docx import legacy_graphics

    blocks = _block_items(_docx_with(_VML_GROUP))

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["image_name"] == legacy_graphics.rendered_image_name(1)
    assert images[0]["data"].startswith(b"\x89PNG")
    assert images[0]["data"] != _noise_png()


@requires_libreoffice
def test_embedded_object_becomes_a_rendered_image() -> None:
    from app.services.document_parser.formats.docx import legacy_graphics

    blocks = _block_items(_docx_with(_VISIO_OBJECT))

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["image_name"] == legacy_graphics.rendered_image_name(1)
    assert not any("not extracted" in text for text in _texts(blocks))


def test_failed_rendering_leaves_a_placeholder_naming_the_object(monkeypatch) -> None:
    from app.services.document_parser.formats.docx import legacy_graphics

    def failing_convert(**_kwargs):
        raise LibreOfficeServiceException(
            internal_message="conversion failed", operation="test"
        )

    monkeypatch.setattr(legacy_graphics, "_convert_with_libreoffice", failing_convert)

    blocks = _block_items(_docx_with(_TEXTLESS_VML_GROUP, _VISIO_OBJECT))

    assert _images(blocks) == []
    assert _texts(blocks) == [
        "",
        "[graphic not extracted]",
        "",
        "[graphic not extracted: Visio.Drawing.11]",
    ]


def test_marked_pages_go_to_their_own_graphic(monkeypatch) -> None:
    from app.services.document_parser.formats.docx import legacy_graphics

    tall, after = _noise_png(), _noise_png()[::-1]
    _fake_rendering(
        monkeypatch,
        [
            ("mark", 0),
            ("page", tall),
            ("page", None),
            ("page", tall),
            ("mark", 1),
            ("page", after),
        ],
    )

    blocks = _block_items(_docx_with(_TEXTLESS_VML_GROUP, _VISIO_OBJECT))

    assert [(image["image_name"], image["data"]) for image in _images(blocks)] == [
        (legacy_graphics.rendered_image_name(1, 1), tall),
        (legacy_graphics.rendered_image_name(1, 2), tall),
        (legacy_graphics.rendered_image_name(2), after),
    ]


def test_graphic_with_blank_pages_alone_keeps_a_placeholder(monkeypatch) -> None:
    _fake_rendering(
        monkeypatch,
        [("mark", 0), ("page", None), ("mark", 1), ("page", _noise_png())],
    )

    blocks = _block_items(_docx_with(_TEXTLESS_VML_GROUP, _VISIO_OBJECT))

    assert len(_images(blocks)) == 1
    assert [text for text in _texts(blocks) if text] == ["[graphic not extracted]"]


def test_marks_out_of_order_keep_every_placeholder(monkeypatch) -> None:
    _fake_rendering(
        monkeypatch,
        [("mark", 1), ("page", _noise_png()), ("mark", 0), ("page", _noise_png())],
    )

    blocks = _block_items(_docx_with(_TEXTLESS_VML_GROUP, _VISIO_OBJECT))

    assert _images(blocks) == []
    assert [text for text in _texts(blocks) if text] == [
        "[graphic not extracted]",
        "[graphic not extracted: Visio.Drawing.11]",
    ]


def test_small_rendered_image_is_dropped_without_placeholder(monkeypatch) -> None:
    _fake_rendering(monkeypatch, [("mark", 0), ("page", _small_png())])

    blocks = _block_items(_docx_with(_TEXTLESS_VML_GROUP))

    assert _images(blocks) == []
    assert not any("not extracted" in text for text in _texts(blocks))


def test_equation_object_is_marked_in_the_text_and_not_rendered(monkeypatch) -> None:
    from app.services.document_parser.formats.docx import legacy_graphics

    def unexpected_render(**_kwargs):
        raise AssertionError("an equation object is not rendered")

    monkeypatch.setattr(legacy_graphics, "_convert_with_libreoffice", unexpected_render)

    blocks = _block_items(
        _docx_with_paragraph_xml(
            '<w:p {ns}><w:r><w:t xml:space="preserve">k = </w:t></w:r>'
            + "<w:r>"
            + _EQUATION_OBJECT.replace("{ns}", "").replace("{rid}", "rIdMissing")
            + '</w:r><w:r><w:t xml:space="preserve">, where k</w:t></w:r></w:p>'
        )
    )

    assert _images(blocks) == []
    assert _texts(blocks) == ["k = [equation not extracted], where k"]


def test_word_equations_are_written_as_latex_in_place() -> None:
    inline = (
        "<m:oMath><m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:oMath>"
    )
    display = (
        "<m:oMathPara><m:oMath><m:f><m:num><m:r><m:t>a</m:t></m:r></m:num>"
        "<m:den><m:r><m:t>b</m:t></m:r></m:den></m:f></m:oMath></m:oMathPara>"
    )
    blocks = _block_items(
        _docx_with_paragraph_xml(
            '<w:p {ns}><w:r><w:t xml:space="preserve">Area is </w:t></w:r>'
            + inline
            + "<w:r><w:t>, so</w:t></w:r>"
            + display
            + "</w:p>"
        )
    )

    assert _texts(blocks) == ["Area is ${x}^{2}$, so\n$$\\frac{a}{b}$$\n"]


def test_deleted_equations_leave_nothing_and_objects_inside_are_marked() -> None:
    deleted = "<m:oMath><w:del><m:r><m:t>i</m:t></m:r></w:del></m:oMath>"
    equation_object = _EQUATION_OBJECT.replace("{ns}", "").replace(
        "{rid}", "rIdMissing"
    )
    holding_object = f"<m:oMath><w:ins><m:r>{equation_object}</m:r></w:ins></m:oMath>"
    holding_deleted_object = (
        f"<m:oMath><w:del><m:r>{equation_object}</m:r></w:del></m:oMath>"
    )
    blocks = _block_items(
        _docx_with_paragraph_xml(
            '<w:p {ns}><w:r><w:t xml:space="preserve">a </w:t></w:r>'
            + deleted
            + holding_object
            + holding_deleted_object
            + '<w:r><w:t xml:space="preserve"> b</w:t></w:r>'
            + f"<m:oMathPara>{deleted}</m:oMathPara></w:p>"
        )
    )

    assert _texts(blocks) == ["a [equation not extracted] b"]


def test_table_cells_take_legacy_pictures_and_equations(monkeypatch) -> None:
    from app.services.document_parser.formats.docx import legacy_graphics
    from app.services.document_parser.formats.docx.table_html import table2html

    rendered = _noise_png()[::-1]
    _fake_rendering(
        monkeypatch,
        [("mark", 0), ("page", rendered), ("mark", 1), ("page", rendered)],
    )
    blocks = _block_items(
        _docx_with(
            _SINGLE_PICTURE,
            _TEXTLESS_VML_GROUP,
            _VISIO_OBJECT,
            _EQUATION_OBJECT,
            in_table=True,
        )
    )

    tables = [(table, meta) for _num, table, label, meta in blocks if label == "TABLE"]
    assert len(tables) == 1
    table, cell_images = tables[0]
    assert {
        cell: [image["image_name"] for image in images]
        for cell, images in cell_images.items()
    } == {
        (0, 0): [cell_images[(0, 0)][0]["image_name"]],
        (0, 1): [legacy_graphics.rendered_image_name(1)],
        (0, 2): [legacy_graphics.rendered_image_name(2)],
    }
    assert cell_images[(0, 0)][0]["data"] == _noise_png()
    assert "<td>[equation not extracted]</td>" in table2html(table)


def test_text_box_text_in_a_content_control_is_read_once() -> None:
    blocks = _block_items(
        _docx_with_paragraph_xml(
            "<w:sdt {ns}><w:sdtContent><w:p><w:r><w:t>Outer</w:t></w:r><w:r>"
            '<w:pict><v:shape style="width:96pt;height:40pt"><v:textbox>'
            "<w:txbxContent><w:p><w:r><w:t>Inner</w:t></w:r></w:p></w:txbxContent>"
            "</v:textbox></v:shape></w:pict></w:r></w:p></w:sdtContent></w:sdt>"
        )
    )

    assert _texts(blocks) == ["Outer", "Inner"]
