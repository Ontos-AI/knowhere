from __future__ import annotations

import io
import os
import zipfile

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest  # noqa: E402
from docx import Document  # noqa: E402
from docx.document import Document as WordDocument  # noqa: E402
from docx.opc.constants import CONTENT_TYPE  # noqa: E402
from docx.table import Table  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402
from lxml.etree import XMLSyntaxError  # noqa: E402
from PIL import Image  # noqa: E402

from app.services.document_parser.formats.docx.block_stream import (  # noqa: E402
    iter_block_items,
)


@pytest.mark.parametrize(
    "content_type",
    [
        CONTENT_TYPE.WML_DOCUMENT_MAIN,
        "application/vnd.ms-word.document.macroEnabled.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    ],
    ids=["document", "macro-enabled-document", "template"],
)
def test_iter_block_items_preserves_body_without_relationships(content_type: str) -> None:
    document: WordDocument = Document()
    document.add_paragraph("Before table")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Table content"
    document.add_paragraph("After table")
    buffer: io.BytesIO = io.BytesIO()
    document.save(buffer)
    package_bytes: bytes = _rewrite_document_package(buffer.getvalue(), None, content_type)

    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        assert "word/_rels/document.xml.rels" not in archive.namelist()

    blocks: list[tuple[int, object, str, object]] = list(iter_block_items(package_bytes))

    assert [block[2] for block in blocks] == ["PTXT", "TABLE", "PTXT"]
    before_table: object = blocks[0][1]
    table: object = blocks[1][1]
    after_table: object = blocks[2][1]
    assert isinstance(before_table, Paragraph)
    assert isinstance(table, Table)
    assert isinstance(after_table, Paragraph)
    assert before_table.text == "Before table"
    assert table.cell(0, 0).text == "Table content"
    assert after_table.text == "After table"


def test_iter_block_items_preserves_image_relationships() -> None:
    image_buffer: io.BytesIO = io.BytesIO()
    Image.new("RGB", (64, 64), color="blue").save(image_buffer, format="BMP")
    image_bytes: bytes = image_buffer.getvalue()
    document: WordDocument = Document()
    document.add_paragraph("Image caption")
    document.add_picture(io.BytesIO(image_bytes))
    buffer: io.BytesIO = io.BytesIO()
    document.save(buffer)

    blocks: list[tuple[int, object, str, object]] = list(iter_block_items(buffer.getvalue()))
    image_blocks: list[tuple[int, object, str, object]] = [
        block for block in blocks if block[2] == "IMAGE"
    ]

    assert len(image_blocks) == 1
    image_metadata: object = image_blocks[0][3]
    assert isinstance(image_metadata, dict)
    assert image_metadata["data"] == image_bytes


def test_iter_block_items_rejects_malformed_relationship_xml() -> None:
    document: WordDocument = Document()
    document.add_paragraph("Sample paragraph")
    buffer: io.BytesIO = io.BytesIO()
    document.save(buffer)
    package_bytes: bytes = _rewrite_document_package(buffer.getvalue(), b"<Relationships")

    with pytest.raises(XMLSyntaxError):
        list(iter_block_items(package_bytes))


def _rewrite_document_package(
    package_bytes: bytes,
    relationships_xml: bytes | None,
    content_type: str = CONTENT_TYPE.WML_DOCUMENT_MAIN,
) -> bytes:
    output: io.BytesIO = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(package_bytes), "r") as source,
        zipfile.ZipFile(output, "w") as destination,
    ):
        for item in source.infolist():
            data: bytes = source.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                if relationships_xml is None:
                    continue
                data = relationships_xml
            elif item.filename == "[Content_Types].xml":
                data = data.replace(
                    CONTENT_TYPE.WML_DOCUMENT_MAIN.encode("ascii"),
                    content_type.encode("ascii"),
                )
            destination.writestr(item, data)
    return output.getvalue()
