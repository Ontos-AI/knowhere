"""Generate a public, synthetic two-page PDF for Codex export tests."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def _footer(page: pymupdf.Page, page_number: int) -> None:
    page.insert_text((72, 760), "Synthetic fixture - no client content", fontsize=8)
    page.insert_text((520, 760), f"Page {page_number}", fontsize=8)


def generate_pdf_fixture(output_path: Path) -> Path:
    """Write a deterministic-layout PDF with headings, tables, image, and footer."""
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    try:
        first = document.new_page(width=612, height=792)
        first.insert_text((72, 72), "Synthetic Technical Review", fontsize=22)
        first.insert_text((72, 112), "1. Scope", fontsize=16)
        first.insert_text(
            (72, 140),
            "Public synthetic values exercise Unicode: ≤, ±, µA, 10⁻⁶, and °C.",
            fontsize=10,
        )
        first.insert_text((72, 180), "1.1 Simple table", fontsize=13)
        for x in (72, 240, 390, 540):
            first.draw_line((x, 200), (x, 280), color=(0, 0, 0))
        for y in (200, 226, 253, 280):
            first.draw_line((72, y), (540, y), color=(0, 0, 0))
        first.insert_text((80, 218), "Metric", fontsize=9)
        first.insert_text((248, 218), "Nominal", fontsize=9)
        first.insert_text((398, 218), "Limit", fontsize=9)
        first.insert_text((80, 245), "Current", fontsize=9)
        first.insert_text((248, 245), "5 µA", fontsize=9)
        first.insert_text((398, 245), "<= 10 µA", fontsize=9)
        first.insert_text((80, 272), "Temperature", fontsize=9)
        first.insert_text((248, 272), "25 °C", fontsize=9)
        first.insert_text((398, 272), "± 2 °C", fontsize=9)
        first.draw_rect((72, 320, 240, 440), color=(0.1, 0.3, 0.8), fill=(0.8, 0.9, 1))
        first.draw_circle((156, 380), 38, color=(0.8, 0.2, 0.1), fill=(1, 0.7, 0.5))
        first.insert_text((92, 460), "Synthetic embedded figure", fontsize=10)
        _footer(first, 1)

        second = document.new_page(width=612, height=792)
        second.insert_text((72, 72), "1.1 Results", fontsize=16)
        second.insert_text(
            (72, 105),
            "A merged and nested table representation is preserved as HTML.",
            fontsize=10,
        )
        second.insert_text((72, 150), "2. Conclusion", fontsize=16)
        second.insert_text(
            (72, 180),
            "Use extracted content for navigation and verify evidence on native pages.",
            fontsize=10,
        )
        _footer(second, 2)
        document.set_metadata(
            {
                "title": "Synthetic Technical Review",
                "author": "Knowhere test fixture",
                "subject": "Public synthetic integration fixture",
            }
        )
        document.save(destination, garbage=4, deflate=True)
    finally:
        document.close()
    return destination


if __name__ == "__main__":
    generate_pdf_fixture(Path(__file__).with_name("synthetic-review.pdf"))
