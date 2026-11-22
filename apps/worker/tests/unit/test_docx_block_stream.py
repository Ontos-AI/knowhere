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


def _docx_with_vml(picture_xml: str) -> bytes:
    """A DOCX whose only paragraph holds ``picture_xml``; ``{rid}`` is the image."""
    document = Document()
    rid, _image = document.part.get_or_add_image(io.BytesIO(_noise_png()))
    run = document.add_paragraph().add_run()
    run._r.append(parse_xml(picture_xml.format(ns=_VML_NAMESPACES, rid=rid)))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_SINGLE_PICTURE = (
    '<w:pict {ns}><v:shape style="width:96pt;height:96pt">'
    '<v:imagedata r:id="{rid}"/></v:shape></w:pict>'
)
_COMPOSITE = (
    '<w:pict {ns}><v:group style="width:240pt;height:120pt" '
    'coordsize="240,120">'
    '<v:shape style="position:absolute;left:0;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    '<v:shape style="position:absolute;left:130;top:40;width:110;height:40">'
    "<v:textbox><w:txbxContent><w:p><w:r><w:t>Caption</w:t></w:r></w:p>"
    "</w:txbxContent></v:textbox></v:shape>"
    "</v:group></w:pict>"
)
_TEXTLESS_COMPOSITE = (
    '<w:pict {ns}><v:group style="width:240pt;height:120pt" coordsize="240,120">'
    '<v:shape style="position:absolute;left:0;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    '<v:shape style="position:absolute;left:120;top:0;width:120;height:120">'
    '<v:imagedata r:id="{rid}"/></v:shape>'
    "</v:group></w:pict>"
)


def test_single_vml_picture_reads_its_media_file() -> None:
    blocks = _block_items(_docx_with_vml(_SINGLE_PICTURE))

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["data"] == _noise_png()
    assert not any("VML graphic" in str(content) for _num, content, _l, _m in blocks)


def _libreoffice_available() -> bool:
    try:
        resolve_libreoffice_binary()
    except LibreOfficeServiceException:
        return False
    return True


@pytest.mark.skipif(not _libreoffice_available(), reason="LibreOffice is not installed")
def test_vml_group_becomes_one_rendered_image() -> None:
    from app.services.document_parser.formats.docx import vml_graphics

    blocks = _block_items(_docx_with_vml(_COMPOSITE))

    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["image_name"] == vml_graphics.composite_image_name(1)
    assert images[0]["data"].startswith(b"\x89PNG")
    assert images[0]["data"] != _noise_png()


def test_vml_group_keeps_placeholder_when_rendering_fails(monkeypatch) -> None:
    from app.services.document_parser.formats.docx import vml_graphics

    def failing_convert(**_kwargs):
        raise LibreOfficeServiceException(
            internal_message="conversion failed", operation="test"
        )

    monkeypatch.setattr(vml_graphics, "_convert_with_libreoffice", failing_convert)

    blocks = _block_items(_docx_with_vml(_TEXTLESS_COMPOSITE))

    assert _images(blocks) == []
    assert any("VML graphic" in str(content) for _num, content, _l, _m in blocks)
