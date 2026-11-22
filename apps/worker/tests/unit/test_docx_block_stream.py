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
from docx.oxml import OxmlElement  # noqa: E402
from PIL import Image  # noqa: E402

from shared.core.constants.processing import ProcessingConstants  # noqa: E402
from app.services.document_parser.formats.docx.block_stream import (  # noqa: E402
    iter_block_items,
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


def test_picture_inside_content_control_is_extracted() -> None:
    blocks = list(iter_block_items(_docx_with_picture_in_content_control()))

    images = [meta for _num, _content, label, meta in blocks if label == "IMAGE"]
    assert len(images) == 1
    assert images[0]["data"].startswith(b"\x89PNG")
