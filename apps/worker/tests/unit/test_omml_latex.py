from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest  # noqa: E402
from lxml import etree  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

from app.services.document_parser.formats.docx.omml_latex import (  # noqa: E402
    omml_to_latex,
)

_M = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'


def _r(text: str) -> str:
    return f"<m:r><m:t>{text}</m:t></m:r>"


def _latex(body: str) -> str:
    return omml_to_latex(etree.fromstring(f"<m:oMath {_M}>{body}</m:oMath>"))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_r("x+1"), "x+1"),
        (
            f"<m:sSub><m:sSubPr/><m:e>{_r('x')}</m:e><m:sub>{_r('i')}</m:sub></m:sSub>",
            "{x}_{i}",
        ),
        (
            f"<m:sSup><m:e>{_r('x')}</m:e><m:sup>{_r('2')}</m:sup></m:sSup>",
            "{x}^{2}",
        ),
        (
            f"<m:sSubSup><m:e>{_r('x')}</m:e><m:sub>{_r('i')}</m:sub>"
            f"<m:sup>{_r('2')}</m:sup></m:sSubSup>",
            "{x}_{i}^{2}",
        ),
        (
            f"<m:sPre><m:sub>{_r('a')}</m:sub><m:sup>{_r('b')}</m:sup>"
            f"<m:e>{_r('X')}</m:e></m:sPre>",
            "{}_{a}^{b}{X}",
        ),
        (
            f"<m:f><m:num>{_r('a')}</m:num><m:den>{_r('b')}</m:den></m:f>",
            r"\frac{a}{b}",
        ),
        (f"<m:d><m:e>{_r('a')}</m:e></m:d>", r"\left(a\right)"),
        (
            '<m:d><m:dPr><m:begChr m:val="{"/><m:endChr m:val=""/></m:dPr>'
            f"<m:e>{_r('a')}</m:e><m:e>{_r('b')}</m:e></m:d>",
            r"\left\{a|b\right.",
        ),
        (
            '<m:d><m:dPr><m:begChr m:val="\uff08"/><m:endChr m:val="\uff09"/>'
            f'<m:sepChr m:val=","/></m:dPr><m:e>{_r("a")}</m:e><m:e>{_r("b")}</m:e></m:d>',
            "\uff08a,b\uff09",
        ),
        (
            '<m:acc><m:accPr><m:chr m:val="\u20d7"/></m:accPr>'
            f"<m:e>{_r('v')}</m:e></m:acc>",
            r"\vec{v}",
        ),
        (f"<m:acc><m:e>{_r('a')}</m:e></m:acc>", r"\hat{a}"),
        (
            '<m:acc><m:accPr><m:chr m:val="\u02dc"/></m:accPr>'
            f"<m:e>{_r('a')}</m:e></m:acc>",
            "\\overset{\u02dc}{a}",
        ),
        (
            '<m:nary><m:naryPr><m:chr m:val="\u2211"/></m:naryPr>'
            f"<m:sub>{_r('i=1')}</m:sub><m:sup>{_r('n')}</m:sup>"
            f"<m:e>{_r('x')}</m:e></m:nary>",
            r"\sum_{i=1}^{n}{x}",
        ),
        (
            "<m:nary><m:naryPr><m:subHide/><m:supHide/></m:naryPr>"
            f"<m:sub/><m:sup/><m:e>{_r('f')}</m:e></m:nary>",
            r"\int{f}",
        ),
        (
            f"<m:eqArr><m:e>{_r('a=1')}</m:e><m:e>{_r('b=2')}</m:e></m:eqArr>",
            r"\begin{aligned}a=1\\b=2\end{aligned}",
        ),
        (
            f"<m:func><m:fName>{_r('sin')}</m:fName><m:e>{_r('x')}</m:e></m:func>",
            r"\operatorname{sin}{x}",
        ),
        (
            f"<m:rad><m:radPr><m:degHide/></m:radPr><m:deg/><m:e>{_r('x')}</m:e></m:rad>",
            r"\sqrt{x}",
        ),
        (
            f"<m:rad><m:deg>{_r('3')}</m:deg><m:e>{_r('x')}</m:e></m:rad>",
            r"\sqrt[3]{x}",
        ),
        (f"<m:borderBox><m:e>{_r('x')}</m:e></m:borderBox>", r"\boxed{x}"),
        (
            f"<m:limLow><m:e>{_r('lim')}</m:e><m:lim>{_r('n')}</m:lim></m:limLow>",
            r"\underset{n}{lim}",
        ),
        (
            f"<m:limUpp><m:e>{_r('x')}</m:e><m:lim>{_r('a')}</m:lim></m:limUpp>",
            r"\overset{a}{x}",
        ),
        (
            f"<m:m><m:mr><m:e>{_r('a')}</m:e><m:e>{_r('b')}</m:e></m:mr>"
            f"<m:mr><m:e>{_r('c')}</m:e><m:e>{_r('d')}</m:e></m:mr></m:m>",
            r"\begin{matrix}a & b\\c & d\end{matrix}",
        ),
        (f"<m:groupChr><m:e>{_r('x')}</m:e></m:groupChr>", r"\underbrace{x}"),
        (
            '<m:groupChr><m:groupChrPr><m:chr m:val="\u2192"/><m:pos m:val="top"/>'
            f"</m:groupChrPr><m:e>{_r('x')}</m:e></m:groupChr>",
            "\\overset{\u2192}{x}",
        ),
        (f"<m:bar><m:e>{_r('x')}</m:e></m:bar>", r"\underline{x}"),
        (
            '<m:bar><m:barPr><m:pos m:val="top"/></m:barPr>'
            f"<m:e>{_r('x')}</m:e></m:bar>",
            r"\overline{x}",
        ),
        (f"<m:box><m:e>{_r('x')}</m:e></m:box>", "x"),
        (f"<m:phant><m:e>{_r('x')}</m:e></m:phant>", "x"),
        (f"<m:unknownThing><m:e>{_r('kept')}</m:e></m:unknownThing>", "kept"),
    ],
)
def test_omml_structures_convert_to_latex(body: str, expected: str) -> None:
    assert _latex(body) == expected


def test_latex_markup_characters_are_escaped() -> None:
    assert _latex(_r("a_b^c {d} 50% #1 &amp; $ \\")) == (
        r"a\_b\^{}c \{d\} 50\% \#1 \& \$ \backslash{}"
    )


def test_word_elements_inside_an_equation_are_read_through() -> None:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        f"<m:sSub><m:e><w:sdt {w}><w:sdtContent>{_r('o')}</w:sdtContent></w:sdt></m:e>"
        f"<m:sub>{_r('1')}</m:sub></m:sSub>"
    )
    assert _latex(body) == "{o}_{1}"
