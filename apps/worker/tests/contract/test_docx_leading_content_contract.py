from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from docx import Document

from app.services.document_parser.formats.docx.parser import (
    convert_doc2dics,
    parse_docx,
)
from app.services.document_parser.formats.text.parser import postprocess_leaf_dics


def test_docx_preserves_four_paragraphs_before_the_first_heading(
    tmp_path: Path,
) -> None:
    document = Document()
    document.add_heading("Document title", level=0)
    references: list[str] = [f"LEADING-PARAGRAPH-{index}" for index in range(1, 5)]
    for reference in references:
        document.add_paragraph(f"{reference} Original introduction body.")
    document.add_heading("First section", level=1)
    document.add_paragraph("SECTION-BODY Original section body.")
    source_path: Path = tmp_path / "introduction.docx"
    document.save(source_path)
    options: dict[str, object] = {
        "doc_type": "book",
        "smart_title_parse": False,
        "summary_image": False,
        "summary_table": False,
        "summary_txt": False,
        "stopwords": [],
    }
    structure, assets = parse_docx(
        str(source_path), options, str(tmp_path), filename=source_path.name
    )
    parsed = convert_doc2dics(
        structure, assets, str(tmp_path), options, relative_root=source_path.name
    )
    body: str = "\n".join(parsed["content"].astype(str))
    for reference in references:
        assert reference in body
    assert "SECTION-BODY" in body
    introduction = parsed[parsed["content"].str.contains(references[0])]
    assert introduction.iloc[0]["path"] == source_path.name


def test_root_body_splitting_preserves_every_leading_paragraph() -> None:
    paragraphs: list[str] = [
        f"ROOT-PARAGRAPH-{index:03d} " + "Original body text. " * 20
        for index in range(60)
    ]
    parsed = postprocess_leaf_dics(
        [([], {"heading": [], "content": paragraphs})],
        {"doc_type": "book", "summary_txt": False},
        summary_len=200,
    )
    contents: list[str] = [text for group in parsed["content_lst"] for text in group]
    assert contents == paragraphs
    assert len(parsed) > 1
