"""Stage-0 page text line schema and PyMuPDF extraction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.document_agent.pdf_text import (
    LineDraft,
    PageTextBands,
    PageTextLine,
    _extract_page_bands_from_pymupdf_page,
    build_page_text_bands,
    line_fingerprint,
)


def test_line_index_region_and_content_splitlines_align() -> None:
    bands = build_page_text_bands(
        (
            LineDraft(text="A\x0cB"),
            LineDraft(text="   "),
            LineDraft(text="Footer", region="footer"),
        )
    )
    assert [line.text for line in bands.lines] == ["A B", "Footer"]
    assert [line.line_index for line in bands.lines] == [0, 1]
    assert bands.content.splitlines() == [line.text for line in bands.lines]
    assert bands.footer == "Footer"
    assert bands.header == ""


def test_mismatched_line_index_raises() -> None:
    with pytest.raises(ValueError, match="line_index"):
        PageTextBands(lines=(PageTextLine(text="A", region="body", line_index=1),))


def test_embedded_splitlines_char_raises() -> None:
    with pytest.raises(ValueError, match="invalid line text"):
        PageTextBands(lines=(PageTextLine(text="A\nB", region="body", line_index=0),))


def test_to_dict_from_any_roundtrip() -> None:
    bands = build_page_text_bands(
        (
            LineDraft(
                text="Title", region="header", height=12.0, font="Helv", font_size=10.0
            ),
            LineDraft(text="Body"),
        )
    )
    restored = PageTextBands.from_any(bands.to_dict())
    assert restored == bands


def test_legacy_cache_dict_without_lines_raises() -> None:
    with pytest.raises(ValueError, match="re-run Stage 0"):
        PageTextBands.from_any({"content": "Body", "header": "H", "footer": "F"})


def test_from_text_content_is_idempotent() -> None:
    bands = build_page_text_bands(
        (
            LineDraft(text="Header", region="header"),
            LineDraft(text="Body"),
        )
    )
    assert PageTextBands.from_text(bands.content).content == bands.content


def test_line_fingerprint_excludes_index_and_uses_render_fields() -> None:
    base = PageTextLine(text="Contents", region="body", line_index=0, height=10.0)
    other_index = PageTextLine(
        text="Contents", region="body", line_index=3, height=10.0
    )
    other_region = PageTextLine(
        text="Contents", region="header", line_index=0, height=10.0
    )
    other_height = PageTextLine(
        text="Contents", region="body", line_index=0, height=20.0
    )
    other_font = PageTextLine(
        text="Contents", region="body", line_index=0, height=10.0, font="Times"
    )
    assert line_fingerprint(base) == line_fingerprint(other_index)
    assert line_fingerprint(base) != line_fingerprint(other_region)
    assert line_fingerprint(base) != line_fingerprint(other_height)
    assert line_fingerprint(base) != line_fingerprint(other_font)


def _span(
    text: str, y0: float, y1: float, *, font: str = "Helv", size: float = 10.0
) -> dict:
    return {
        "text": text,
        "bbox": [0.0, y0, 10.0, y1],
        "font": font,
        "size": size,
    }


def _page(blocks: list[dict], *, height: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        rect=SimpleNamespace(height=height),
        get_text=lambda _mode: {"blocks": blocks},
    )


def test_extract_boundary_center_is_body() -> None:
    header_on_line = _page(
        [{"type": 0, "lines": [{"spans": [_span("OnHeaderLine", 8.0, 12.0)]}]}]
    )
    header_inside = _page(
        [{"type": 0, "lines": [{"spans": [_span("InHeader", 7.0, 11.0)]}]}]
    )
    footer_on_line = _page(
        [{"type": 0, "lines": [{"spans": [_span("OnFooterLine", 88.0, 92.0)]}]}]
    )
    footer_inside = _page(
        [{"type": 0, "lines": [{"spans": [_span("InFooter", 89.0, 93.0)]}]}]
    )

    assert (
        _extract_page_bands_from_pymupdf_page(
            header_on_line, header_y=0.1, footer_y=0.9
        )
        .lines[0]
        .region
        == "body"
    )
    assert (
        _extract_page_bands_from_pymupdf_page(header_inside, header_y=0.1, footer_y=0.9)
        .lines[0]
        .region
        == "header"
    )
    assert (
        _extract_page_bands_from_pymupdf_page(
            footer_on_line, header_y=0.1, footer_y=0.9
        )
        .lines[0]
        .region
        == "body"
    )
    assert (
        _extract_page_bands_from_pymupdf_page(footer_inside, header_y=0.1, footer_y=0.9)
        .lines[0]
        .region
        == "footer"
    )


def test_extract_block_height_is_union_of_kept_lines() -> None:
    page = _page(
        [
            {
                "type": 0,
                "lines": [
                    {"spans": [_span("One", 20.0, 25.0)]},
                    {"spans": [_span("Two", 30.0, 35.0)]},
                    {"spans": [_span("Three", 45.0, 50.0)]},
                ],
            }
        ]
    )
    bands = _extract_page_bands_from_pymupdf_page(page, header_y=None, footer_y=None)
    assert [line.height for line in bands.lines] == [30.0, 30.0, 30.0]


def test_extract_font_uses_longest_span() -> None:
    page = _page(
        [
            {
                "type": 0,
                "lines": [
                    {
                        "spans": [
                            _span("ab", 20.0, 24.0, font="Short", size=8.0),
                            _span("longer", 20.0, 24.0, font="Long", size=12.5),
                        ]
                    }
                ],
            }
        ]
    )
    line = _extract_page_bands_from_pymupdf_page(
        page, header_y=None, footer_y=None
    ).lines[0]
    assert line.font == "Long"
    assert line.font_size == 12.5


def test_extract_blank_span_line_is_dropped_from_height() -> None:
    page = _page(
        [
            {
                "type": 0,
                "lines": [
                    {"spans": [_span("   ", 10.0, 12.0)]},
                    {"spans": [_span("Keep", 20.0, 30.0)]},
                ],
            }
        ]
    )
    bands = _extract_page_bands_from_pymupdf_page(page, header_y=None, footer_y=None)
    assert [line.text for line in bands.lines] == ["Keep"]
    assert bands.lines[0].height == 10.0
