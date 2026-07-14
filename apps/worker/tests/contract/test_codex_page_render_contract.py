from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pymupdf
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.codex_export.docx_render import render_docx_to_normalized_pdf
from app.services.codex_export.page_selection import (
    render_review_pages,
    resolve_selected_pages,
)
from app.services.codex_export.schema import (
    BLOCK_SCHEMA_VERSION,
    DocumentBlock,
    canonical_sha256,
)
from app.services.page_memory.page_renderer import PageRenderResult
from shared.core.exceptions.domain_exceptions import LibreOfficeServiceException


def _block(block_type: str, page: int, *, locator_kind: str = "pdf_page") -> DocumentBlock:
    content = {"value": block_type}
    locator = {
        "kind": locator_kind,
        "page_index": page - 1,
        "page_number": page,
        "block_index": 0,
    }
    if locator_kind == "office_logical_page":
        locator["normalized_pdf_page_number"] = None
        locator["normalized_pdf_mapping_status"] = "unmapped"
    return DocumentBlock(
        schema_version=BLOCK_SCHEMA_VERSION,
        document_id="doc_pages",
        block_id=f"blk_{block_type}_{page}",
        sequence=page,
        block_type=block_type,
        text=block_type,
        structured_content=content,
        content_sha256=canonical_sha256(content),
        section={"node_id": "sec_root", "path": [], "heading_level": 0},
        source_locator=locator,
        assets=[],
        provenance={},
        flags=[],
    )


def _pdf(path: Path, page_count: int) -> None:
    document = pymupdf.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"Synthetic page {page_number}")
    document.save(path)
    document.close()


def test_requested_pdf_pages_and_table_pages_are_sorted_and_deduplicated() -> None:
    blocks = [_block("table", 3), _block("table", 2), _block("paragraph", 4)]

    pages = resolve_selected_pages(
        requested_pages=[3, 1, 3],
        blocks=blocks,
        include_table_pages=True,
        include_image_pages=False,
        page_count=4,
    )

    assert pages == [1, 2, 3]


def test_image_and_chart_pages_are_optional_for_native_pdf() -> None:
    blocks = [_block("image", 4), _block("chart", 2), _block("table", 3)]

    pages = resolve_selected_pages(
        requested_pages=[1],
        blocks=blocks,
        include_table_pages=False,
        include_image_pages=True,
        page_count=4,
    )

    assert pages == [1, 2, 4]


def test_docx_uses_only_explicit_normalized_pdf_pages() -> None:
    blocks = [
        _block("table", 2, locator_kind="office_logical_page"),
        _block("image", 3, locator_kind="office_logical_page"),
    ]

    pages = resolve_selected_pages(
        requested_pages=[1],
        blocks=blocks,
        include_table_pages=True,
        include_image_pages=True,
        page_count=4,
    )

    assert pages == [1]
    assert blocks[0].source_locator["normalized_pdf_page_number"] is None
    assert blocks[0].source_locator["normalized_pdf_mapping_status"] == "unmapped"


@pytest.mark.parametrize("requested", [[0], [-1], [4]])
def test_invalid_requested_page_fails(requested: list[int]) -> None:
    with pytest.raises(ValueError, match="page"):
        resolve_selected_pages(
            requested_pages=requested,
            blocks=[],
            include_table_pages=False,
            include_image_pages=False,
            page_count=3,
        )


def test_auto_selected_page_beyond_page_count_fails() -> None:
    with pytest.raises(ValueError, match="page"):
        resolve_selected_pages(
            requested_pages=[],
            blocks=[_block("table", 5)],
            include_table_pages=True,
            include_image_pages=False,
            page_count=3,
        )


def test_rendered_page_filenames_are_zero_padded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "report.pdf"
    _pdf(pdf_path, 2)
    output_dir = tmp_path / "pages"
    source_renders = tmp_path / "source-renders"
    source_renders.mkdir()
    for page in (1, 2):
        (source_renders / f"source-{page}.png").write_bytes(f"png-{page}".encode())

    from app.services.codex_export import page_selection

    def fake_render_document_pages(**kwargs: Any) -> list[PageRenderResult]:
        assert kwargs["pages"] == [1, 2]
        assert kwargs["dpi"] == 200
        return [
            PageRenderResult(
                page_index=page,
                image_path=str(source_renders / f"source-{page}.png"),
                raw_text="",
                width=612,
                height=792,
                is_landscape=False,
            )
            for page in (1, 2)
        ]

    monkeypatch.setattr(
        page_selection, "render_document_pages", fake_render_document_pages
    )

    rendered = render_review_pages(
        pdf_path=pdf_path,
        pages=[2, 1],
        output_dir=output_dir,
        dpi=200,
    )

    assert [item.output_path.name for item in rendered] == [
        "page-0001.png",
        "page-0002.png",
    ]
    assert [item.output_path.read_bytes() for item in rendered] == [
        b"png-1",
        b"png-2",
    ]


@pytest.mark.parametrize("dpi", [71, 301])
def test_rendering_rejects_dpi_outside_supported_range(
    tmp_path: Path,
    dpi: int,
) -> None:
    pdf_path = tmp_path / "report.pdf"
    _pdf(pdf_path, 1)

    with pytest.raises(ValueError, match="DPI"):
        render_review_pages(
            pdf_path=pdf_path,
            pages=[1],
            output_dir=tmp_path / "pages",
            dpi=dpi,
        )


def test_docx_conversion_uses_argv_list_and_shell_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docx_path = tmp_path / "input files" / "report.docx"
    docx_path.parent.mkdir()
    docx_path.write_bytes(b"PK synthetic docx")
    output_dir = tmp_path / "normalized output"
    fake_binary = tmp_path / "LibreOffice" / "soffice.exe"
    fake_binary.parent.mkdir()
    fake_binary.touch()
    seen: dict[str, Any] = {}
    from app.services.codex_export import docx_render

    monkeypatch.setattr(
        docx_render, "resolve_libreoffice_binary", lambda: str(fake_binary)
    )

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.pdf").write_bytes(b"%PDF synthetic")
        return subprocess.CompletedProcess(argv, 0, stdout="converted", stderr="")

    monkeypatch.setattr(docx_render.subprocess, "run", fake_run)

    normalized_pdf = render_docx_to_normalized_pdf(
        docx_path=docx_path,
        output_dir=output_dir,
    )

    assert isinstance(seen["argv"], list)
    assert seen["argv"][0] == str(fake_binary)
    assert "--headless" in seen["argv"]
    assert seen["kwargs"]["shell"] is False
    assert normalized_pdf == output_dir / "source.pdf"
    assert normalized_pdf.read_bytes() == b"%PDF synthetic"


def test_missing_libreoffice_is_surfaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docx_path = tmp_path / "report.docx"
    docx_path.write_bytes(b"PK synthetic docx")
    from app.services.codex_export import docx_render

    def missing_binary() -> str:
        raise LibreOfficeServiceException(
            internal_message="LibreOffice binary not found",
            operation="resolve_binary",
        )

    monkeypatch.setattr(docx_render, "resolve_libreoffice_binary", missing_binary)

    with pytest.raises(LibreOfficeServiceException, match="LibreOffice"):
        render_docx_to_normalized_pdf(
            docx_path=docx_path,
            output_dir=tmp_path / "normalized",
        )
