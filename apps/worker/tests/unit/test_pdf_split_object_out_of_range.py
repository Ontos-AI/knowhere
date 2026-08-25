"""PDF split retries once after MuPDF xref copy failure."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from app.services.document_parser.formats.pdf.shard_splitter import (
    MergedShard,
    _is_source_object_out_of_range,
    split_pdf,
)

_OBJECT_OUT_OF_RANGE = RuntimeError("code=4: source object number out of range")
_CUSTOMER_PDF = Path(
    "/home/suguan/.cursor/Exercise_Prescription_in_Cardiac_Rehabilitation.pdf"
)


def _write_pages(path: Path, page_count: int) -> None:
    doc = pymupdf.open()
    for index in range(page_count):
        page = doc.new_page(width=72, height=72)
        page.insert_text((12, 24), f"page-{index + 1}")
    doc.save(path)
    doc.close()


def _page_count(path: str) -> int:
    doc = pymupdf.open(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def test_source_object_matcher_is_specific() -> None:
    assert _is_source_object_out_of_range(_OBJECT_OUT_OF_RANGE) is True
    assert (
        _is_source_object_out_of_range(RuntimeError("code=7: syntax error")) is False
    )
    assert _is_source_object_out_of_range(ValueError(_OBJECT_OUT_OF_RANGE.args[0])) is False


def test_split_pdf_copies_pages(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pages(source, 3)
    work_dir = tmp_path / "shards"
    work_dir.mkdir()

    paths, remap = split_pdf(
        str(source),
        [MergedShard(0, 1, 2), MergedShard(1, 3, 3)],
        str(work_dir),
    )

    assert remap is None
    assert [Path(path).name for path in paths] == ["shard_0.pdf", "shard_1.pdf"]
    assert _page_count(paths[0]) == 2
    assert _page_count(paths[1]) == 1
    assert not (work_dir / "_rewritten_source.pdf").exists()


def test_split_pdf_rewrites_once_on_object_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    _write_pages(source, 2)
    work_dir = tmp_path / "shards"
    work_dir.mkdir()

    real_insert = pymupdf.Document.insert_pdf
    calls = {"n": 0}

    def _insert_pdf(self: pymupdf.Document, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _OBJECT_OUT_OF_RANGE
        real_insert(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Document, "insert_pdf", _insert_pdf)

    paths, remap = split_pdf(
        str(source),
        [MergedShard(0, 1, 2)],
        str(work_dir),
    )

    assert remap is None
    assert calls["n"] == 3  # fail once, then copy both pages from rewritten source
    assert (work_dir / "_rewritten_source.pdf").exists()
    assert len(paths) == 1
    assert _page_count(paths[0]) == 2


def test_split_pdf_does_not_catch_other_runtime_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    _write_pages(source, 1)
    work_dir = tmp_path / "shards"
    work_dir.mkdir()

    def _insert_pdf(self: pymupdf.Document, *args: object, **kwargs: object) -> None:
        raise RuntimeError("code=7: syntax error")

    monkeypatch.setattr(pymupdf.Document, "insert_pdf", _insert_pdf)

    with pytest.raises(RuntimeError, match="syntax error"):
        split_pdf(str(source), [MergedShard(0, 1, 1)], str(work_dir))

    assert not (work_dir / "_rewritten_source.pdf").exists()


@pytest.mark.skipif(not _CUSTOMER_PDF.exists(), reason="local Quartz incremental PDF not present")
def test_split_incremental_quartz_pdf_around_failing_page(tmp_path: Path) -> None:
    work_dir = tmp_path / "shards"
    work_dir.mkdir()

    paths, remap = split_pdf(
        str(_CUSTOMER_PDF),
        [MergedShard(0, 40, 50)],
        str(work_dir),
    )

    assert remap is None
    assert len(paths) == 1
    assert _page_count(paths[0]) == 11
    assert (work_dir / "_rewritten_source.pdf").exists()
