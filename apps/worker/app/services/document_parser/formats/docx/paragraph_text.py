"""Text of a DOCX paragraph, with its equations in place.

python-docx reads a paragraph's runs and hyperlinks only, so Word equations
drop out. This reads the same runs and hyperlinks the same way and writes an
equation where it stands: inline as ``$...$``, on a line of its own as
``$$...$$``. An embedded equation object (Equation Editor 3.0, MathType) has
no readable content, so its place is marked.
"""

from __future__ import annotations

from docx.oxml.ns import qn
from docx.table import _Cell
from docx.text.paragraph import Paragraph

from app.services.document_parser.formats.docx.legacy_graphics import (
    is_equation_object,
)
from app.services.document_parser.formats.docx.omml_latex import (
    REMOVED_TAGS,
    omml_to_latex,
)

EQUATION_OBJECT_MARK = "[equation not extracted]"

_RUN_TAGS = (qn("w:r"), qn("w:hyperlink"))
_OBJECT_TAG = qn("w:object")
_MATH_TAG = qn("m:oMath")
_MATH_PARAGRAPH_TAG = qn("m:oMathPara")


def paragraph_text(paragraph: Paragraph) -> str:
    parts: list[str] = []
    for child in paragraph._p.iterchildren():
        if child.tag in _RUN_TAGS:
            parts.append(child.text)
            parts.extend(
                EQUATION_OBJECT_MARK
                for obj in child.iter(_OBJECT_TAG)
                if is_equation_object(obj)
            )
        elif child.tag == _MATH_TAG:
            parts.append(_equation_text(child, "$"))
        elif child.tag == _MATH_PARAGRAPH_TAG:
            for o_math in child.iterchildren(_MATH_TAG):
                text = _equation_text(o_math, "$$")
                if text:
                    parts.append(f"\n{text}\n")
    return "".join(parts)


def cell_text(cell: _Cell) -> str:
    """A table cell's text, joined by paragraph as python-docx joins it."""
    return "\n".join(paragraph_text(paragraph) for paragraph in cell.paragraphs)


def _equation_text(o_math, delimiter: str) -> str:
    """An equation between delimiters, then a mark for each equation object in it.

    An equation whose content is all tracked deletions reads as nothing and
    leaves nothing.
    """
    latex = omml_to_latex(o_math)
    marks = [
        EQUATION_OBJECT_MARK
        for obj in o_math.iter(_OBJECT_TAG)
        if is_equation_object(obj)
        and not any(ancestor.tag in REMOVED_TAGS for ancestor in obj.iterancestors())
    ]
    text = f"{delimiter}{latex}{delimiter}" if latex.strip() else ""
    return text + "".join(marks)
