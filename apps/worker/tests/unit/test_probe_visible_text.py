"""Probe text metrics must ignore PDF invisible ink (``3 Tr``)."""

from __future__ import annotations

from pathlib import Path

import fitz

from app.services.document_agent.tools.probe_page_features import (
    _probe_text_lengths,
    _probe_text_one,
)

_VISIBLE_SAMPLE = "VisibleBodyTextXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
_INVISIBLE_SAMPLE = "InvisibleOCRLayer"


def _write_mixed_visibility_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=300)

    invisible = fitz.TextWriter(page.rect)
    invisible.append((40, 80), _INVISIBLE_SAMPLE)
    invisible.write_text(page, render_mode=3)

    visible = fitz.TextWriter(page.rect)
    visible.append((40, 160), _VISIBLE_SAMPLE)
    visible.write_text(page, render_mode=0)

    doc.save(path)
    doc.close()


def _write_invisible_only_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=300)
    writer = fitz.TextWriter(page.rect)
    writer.append((40, 80), "OnlyInvisibleInk")
    writer.write_text(page, render_mode=3)
    doc.save(path)
    doc.close()


def test_probe_text_lengths_splits_visible_and_invisible(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed_tr.pdf"
    _write_mixed_visibility_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        extracted = page.get_text() or ""
        assert _INVISIBLE_SAMPLE in extracted
        assert "VisibleBodyText" in extracted

        visible_len, invisible_len = _probe_text_lengths(page)
    finally:
        doc.close()

    assert visible_len == len(_VISIBLE_SAMPLE)
    assert invisible_len == len(_INVISIBLE_SAMPLE)
    assert len(_VISIBLE_SAMPLE) >= 50


def test_probe_text_one_uses_visible_length_only(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed_tr.pdf"
    _write_mixed_visibility_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        feature = _probe_text_one(doc[0], 1)
    finally:
        doc.close()

    assert len(_VISIBLE_SAMPLE) >= 50
    assert feature["raw_text_length"] == len(_VISIBLE_SAMPLE)
    assert feature["invisible_text_length"] == len(_INVISIBLE_SAMPLE)
    assert feature["is_blank_like"] is False


def test_invisible_only_page_counts_as_empty_for_probe(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invisible_only.pdf"
    _write_invisible_only_pdf(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        assert len((page.get_text() or "").strip()) == len("OnlyInvisibleInk")
        feature = _probe_text_one(page, 1)
    finally:
        doc.close()

    assert feature["raw_text_length"] == 0
    assert feature["invisible_text_length"] == len("OnlyInvisibleInk")
    assert feature["text_density"] == 0.0
    assert feature["is_blank_like"] is True
