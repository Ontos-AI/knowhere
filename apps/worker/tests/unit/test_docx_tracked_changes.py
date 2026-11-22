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

from docx import Document  # noqa: E402
from lxml import etree  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

from app.services.document_parser.formats.docx.tracked_changes import (  # noqa: E402
    accept_tracked_changes,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_NS = {"w": _W, "m": _M}
_DECLARE = f'xmlns:w="{_W}" xmlns:m="{_M}"'


def _package(body_xml: str, header_xml: str | None = None) -> bytes:
    """A DOCX whose body holds ``body_xml``, and a header holding ``header_xml``."""
    document = Document()
    if header_xml is not None:
        document.sections[0].header.paragraphs[0].text = "header"
    buffer = io.BytesIO()
    document.save(buffer)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as source:
        with zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "word/document.xml":
                    data = f"<w:document {_DECLARE}><w:body>{body_xml}</w:body></w:document>".encode()
                elif item.filename.startswith("word/header") and header_xml is not None:
                    data = f"<w:hdr {_DECLARE}>{header_xml}</w:hdr>".encode()
                target.writestr(item, data)
    return output.getvalue()


def _part(package: bytes, prefix: str = "word/document.xml"):
    with zipfile.ZipFile(io.BytesIO(package)) as source:
        name = next(name for name in source.namelist() if name.startswith(prefix))
        return etree.fromstring(source.read(name))


def _accepted_body(body_xml: str):
    return _part(accept_tracked_changes(_package(body_xml))).find("w:body", _NS)


def _paragraph_texts(element) -> list[str]:
    return [
        "".join(paragraph.xpath(".//w:t/text() | .//m:t/text()", namespaces=_NS))
        for paragraph in element.iter(f"{{{_W}}}p")
    ]


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _deleted_run(text: str) -> str:
    return f"<w:r><w:delText>{text}</w:delText></w:r>"


def test_inserted_content_stays_and_deleted_content_goes() -> None:
    body = _accepted_body(
        "<w:p>"
        + _run("kept ")
        + f"<w:ins>{_run('inserted ')}</w:ins>"
        + f"<w:del>{_deleted_run('deleted ')}</w:del>"
        + f"<w:moveTo>{_run('moved in')}</w:moveTo>"
        + f"<w:moveFrom>{_run('moved away')}</w:moveFrom>"
        + "</w:p>"
    )

    assert _paragraph_texts(body) == ["kept inserted moved in"]
    revisions = body.xpath(
        ".//w:ins | .//w:del | .//w:moveTo | .//w:moveFrom", namespaces=_NS
    )
    assert revisions == []


def test_a_paragraph_with_a_deleted_mark_joins_the_next_one() -> None:
    deleted_mark = "<w:pPr><w:pStyle w:val='Heading1'/><w:rPr><w:del/></w:rPr></w:pPr>"
    body = _accepted_body(
        f"<w:p>{deleted_mark}{_run('first, ')}</w:p>"
        f"<w:p>{deleted_mark}<w:del>{_deleted_run('gone')}</w:del></w:p>"
        f"<w:p><w:pPr><w:pStyle w:val='Normal'/></w:pPr>{_run('second')}</w:p>"
    )

    paragraphs = body.findall("w:p", _NS)
    assert _paragraph_texts(body) == ["first, second"]
    assert paragraphs[0].xpath("w:pPr/w:pStyle/@w:val", namespaces=_NS) == ["Normal"]


def test_a_paragraph_with_a_deleted_mark_and_nothing_to_join_stays() -> None:
    body = _accepted_body(
        "<w:tbl><w:tr><w:tc>"
        f"<w:p><w:pPr><w:rPr><w:del/></w:rPr></w:pPr>{_run('last in cell')}</w:p>"
        "</w:tc></w:tr></w:tbl>"
        f"<w:p><w:pPr><w:rPr><w:del/></w:rPr></w:pPr>{_run('before a table')}</w:p>"
        "<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>"
    )

    assert _paragraph_texts(body) == ["last in cell", "before a table", ""]


def test_deleted_rows_and_cells_are_removed() -> None:
    deleted_row = f"<w:tr><w:trPr><w:del/></w:trPr><w:tc><w:p>{_run('row gone')}</w:p></w:tc></w:tr>"
    body = _accepted_body(
        "<w:tbl>"
        + f"<w:tr><w:trPr><w:ins/></w:trPr><w:tc><w:p>{_run('row kept')}</w:p></w:tc>"
        + f"<w:tc><w:tcPr><w:cellDel/></w:tcPr><w:p>{_run('cell gone')}</w:p></w:tc></w:tr>"
        + deleted_row
        + f"<w:sdt><w:sdtContent>{deleted_row}</w:sdtContent></w:sdt>"
        + "</w:tbl>"
        + f"<w:tbl>{deleted_row}</w:tbl>"
    )

    assert len(body.findall("w:tbl", _NS)) == 1
    assert _paragraph_texts(body) == ["row kept"]
    assert body.xpath(".//w:trPr/*", namespaces=_NS) == []


def test_deleted_equation_structures_leave_no_skeleton() -> None:
    deleted_control = "<m:ctrlPr><w:ins><w:del><w:rPr/></w:del></w:ins></m:ctrlPr>"
    argument_control = "<m:ctrlPr><w:del><w:rPr/></w:del></m:ctrlPr>"
    body = _accepted_body(
        "<w:p><m:oMath>"
        # The whole subscript was deleted: its characters and its structure.
        f"<m:sSub><m:sSubPr>{deleted_control}</m:sSubPr>"
        f"<m:e><w:del><m:r><m:t>o</m:t></m:r></w:del></m:e>"
        f"<m:sub><w:del><m:r><m:t>1</m:t></m:r></w:del></m:sub></m:sSub>"
        # A kept fraction whose argument carries a deleted control character.
        f"<m:f><m:fPr><m:ctrlPr><w:rPr/></m:ctrlPr></m:fPr>"
        f"<m:num><m:r><m:t>a</m:t></m:r>{argument_control}</m:num>"
        f"<m:den><m:r><m:t>b</m:t></m:r></m:den></m:f>"
        "</m:oMath></w:p>"
    )

    math = body.find(".//m:oMath", _NS)
    assert [etree.QName(child).localname for child in math] == ["f"]
    assert _paragraph_texts(body) == ["ab"]


def test_formatting_history_and_move_ranges_are_dropped() -> None:
    body = _accepted_body(
        "<w:p><w:pPr><w:jc w:val='center'/><w:pPrChange><w:pPr/></w:pPrChange></w:pPr>"
        "<w:moveToRangeStart w:id='1' w:name='move'/>"
        "<w:r><w:rPr><w:b/><w:rPrChange><w:rPr/></w:rPrChange></w:rPr><w:t>bold</w:t></w:r>"
        "<w:moveToRangeEnd w:id='1'/></w:p>"
    )

    paragraph = body.find("w:p", _NS)
    assert [etree.QName(e).localname for e in paragraph.iter()] == [
        "p",
        "pPr",
        "jc",
        "r",
        "rPr",
        "b",
        "t",
    ]


def test_headers_are_accepted_too() -> None:
    package = accept_tracked_changes(
        _package(
            f"<w:p>{_run('body')}</w:p>",
            header_xml=f"<w:p><w:del>{_deleted_run('old header')}</w:del>{_run('header')}</w:p>",
        )
    )

    assert _paragraph_texts(_part(package, "word/header")) == ["header"]


def test_a_package_without_tracked_changes_comes_back_as_it_is() -> None:
    package = _package(f"<w:p>{_run('plain')}</w:p>")

    assert accept_tracked_changes(package) is package
    assert accept_tracked_changes(b"not a zip package") == b"not a zip package"
