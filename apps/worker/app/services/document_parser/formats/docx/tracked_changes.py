"""A Word package as it reads once every tracked change is accepted.

Word keeps tracked changes inside the document XML (ECMA-376 Part 1, section
17.13.5): deleted and moved-away content stays in place under a deletion mark,
inserted and moved-in content sits inside an insertion mark, and a formatting
change keeps the old formatting beside the new one. Accepting every change
here, before anything reads the package, keeps deleted content and revision
marks out of the text, the tables, the pictures and the rendered pages alike.

Accepting a change follows Word:

- deleted and moved-away content is removed; inserted and moved-in content
  stays as ordinary content;
- a paragraph whose paragraph mark is deleted joins the paragraph after it,
  which keeps its own paragraph properties; without a paragraph to join it
  stays where it is;
- a deleted table row is removed, and a table left without rows with it;
  a deleted table cell is removed;
- an equation structure (fraction, subscript, ...) whose own control
  character is deleted is removed with its arguments;
- formatting history and move ranges are dropped.
"""

from __future__ import annotations

import io
import zipfile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_NS = {"w": _W_NS, "m": _M_NS}


def _w(name: str) -> str:
    return f"{{{_W_NS}}}{name}"


_REMOVED = (_w("del"), _w("moveFrom"))
_KEPT = (_w("ins"), _w("moveTo"))
_MARKS = (
    _w("cellIns"),
    _w("moveFromRangeStart"),
    _w("moveFromRangeEnd"),
    _w("moveToRangeStart"),
    _w("moveToRangeEnd"),
    _w("rPrChange"),
    _w("pPrChange"),
    _w("sectPrChange"),
    _w("tblPrChange"),
    _w("tblPrExChange"),
    _w("trPrChange"),
    _w("tcPrChange"),
    _w("tblGridChange"),
    _w("numberingChange"),
)
_REVISIONS = (*_REMOVED, *_KEPT, _w("cellDel"), *_MARKS)
# Elements that stand as blocks in a body, a cell or a content control.
_BLOCKS = (
    _w("p"),
    _w("tbl"),
    _w("sdt"),
    _w("customXml"),
    _w("altChunk"),
    _w("sectPr"),
)
_WORD_NS_BYTES = _W_NS.encode()


def accept_tracked_changes(package_bytes: bytes) -> bytes:
    """The package with every tracked change accepted.

    A package without tracked changes comes back as it is. Bytes that are not
    a ZIP package have no OOXML tracked changes and come back as they are too.
    """
    if not zipfile.is_zipfile(io.BytesIO(package_bytes)):
        return package_bytes
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as source:
        accepted: dict[str, bytes] = {}
        for item in source.infolist():
            if not item.filename.endswith(".xml"):
                continue
            data = source.read(item.filename)
            if _WORD_NS_BYTES not in data:
                continue
            root = etree.fromstring(data)
            if _accept(root):
                accepted[item.filename] = etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
        if not accepted:
            return package_bytes
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                target.writestr(
                    item, accepted.get(item.filename) or source.read(item.filename)
                )
    return output.getvalue()


def _accept(root) -> bool:
    """Accept the tracked changes in one XML part; whether it had any."""
    if next(root.iter(*_REVISIONS), None) is None:
        return False
    for structure in _deleted_equation_structures(root):
        _remove(structure)
    shortened = []
    for row in root.xpath(".//w:tr[w:trPr/w:del]", namespaces=_NS):
        shortened.append(_table_of(row))
        _remove(row)
    for table in dict.fromkeys(shortened):
        if not any(_table_of(row) is table for row in table.iter(_w("tr"))):
            _remove(table)
    for cell in root.xpath(".//w:tc[w:tcPr/w:cellDel]", namespaces=_NS):
        _remove(cell)
    joining = root.xpath(
        ".//w:p[w:pPr/w:rPr/w:del or w:pPr/w:rPr/w:moveFrom]", namespaces=_NS
    )
    for element in list(root.iter(*_REMOVED)):
        _remove(element)
    for element in list(root.iter(*_KEPT)):
        parent = element.getparent()
        index = parent.index(element)
        parent[index : index + 1] = list(element)
    for paragraph in joining:
        _join_next_paragraph(paragraph)
    for element in list(root.iter(*_MARKS)):
        _remove(element)
    return True


def _deleted_equation_structures(root) -> list:
    """Equation structures whose own control character is deleted.

    A structure's control properties sit in its properties element, named
    after the structure (``m:sSubPr`` in ``m:sSub``); control properties inside
    an argument belong to that argument, not to the structure.
    """
    structures = []
    for control in root.iter(f"{{{_M_NS}}}ctrlPr"):
        properties = control.getparent()
        structure = properties.getparent()
        if etree.QName(properties).localname == (
            f"{etree.QName(structure).localname}Pr"
        ) and control.xpath(".//w:del | .//w:moveFrom", namespaces=_NS):
            structures.append(structure)
    return structures


def _table_of(row):
    """The table a row belongs to; a content control may stand between them."""
    return row.xpath("ancestor::w:tbl[1]", namespaces=_NS)[0]


def _join_next_paragraph(paragraph) -> None:
    """Move a paragraph's content to the start of the paragraph after it."""
    following = next(
        (block for block in paragraph.itersiblings() if block.tag in _BLOCKS), None
    )
    if following is None or following.tag != _w("p"):
        return
    content = [child for child in paragraph if child.tag != _w("pPr")]
    start = 1 if following.find("w:pPr", _NS) is not None else 0
    following[start:start] = content
    _remove(paragraph)


def _remove(element) -> None:
    element.getparent().remove(element)
