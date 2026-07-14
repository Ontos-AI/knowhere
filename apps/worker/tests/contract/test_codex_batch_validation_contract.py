from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.codex_export.validation_corpus import load_validation_corpus


def _write_corpus(path: Path, documents: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "codex-validation-corpus/1.0",
                "documents": documents,
            }
        ),
        encoding="utf-8",
    )
    return path


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "id": "sample-pdf",
        "root": "knowhere",
        "path": "fixtures/sample.pdf",
        "tags": ["pdf", "public-test"],
        "language": "en",
        "pages": [1],
        "expected_status": "completed",
    }
    document.update(overrides)
    return document


def test_load_validation_corpus_resolves_safe_relative_document(tmp_path: Path) -> None:
    root = tmp_path / "knowhere"
    source = root / "fixtures" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-test")
    corpus_path = _write_corpus(tmp_path / "corpus.json", [_document()])

    corpus = load_validation_corpus(corpus_path, roots={"knowhere": root})

    assert corpus.schema_version == "codex-validation-corpus/1.0"
    assert len(corpus.documents) == 1
    loaded = corpus.documents[0]
    assert loaded.document_id == "sample-pdf"
    assert loaded.root_name == "knowhere"
    assert loaded.relative_path.as_posix() == "fixtures/sample.pdf"
    assert loaded.tags == ("pdf", "public-test")
    assert loaded.language == "en"
    assert loaded.pages == (1,)
    assert loaded.expected_status == "completed"
    assert loaded.source_path == source.resolve()


@pytest.mark.parametrize(
    ("documents", "message"),
    [
        ([_document(path="C:/private/sample.pdf")], "relative"),
        ([_document(path="../sample.pdf")], "escape"),
        ([_document(path="fixtures/sample.txt")], "PDF or DOCX"),
        ([_document(id="INVALID ID")], "document id"),
        ([_document(pages=[0])], "positive"),
        ([_document(expected_status="unknown")], "expected_status"),
        ([_document(), _document()], "duplicate"),
    ],
)
def test_load_validation_corpus_rejects_unsafe_records(
    tmp_path: Path,
    documents: list[dict[str, object]],
    message: str,
) -> None:
    root = tmp_path / "knowhere"
    source = root / "fixtures" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-test")
    corpus_path = _write_corpus(tmp_path / "corpus.json", documents)

    with pytest.raises(ValueError, match=message):
        load_validation_corpus(corpus_path, roots={"knowhere": root})


def test_load_validation_corpus_rejects_missing_document(tmp_path: Path) -> None:
    root = tmp_path / "knowhere"
    root.mkdir()
    corpus_path = _write_corpus(tmp_path / "corpus.json", [_document()])

    with pytest.raises(ValueError, match="does not exist"):
        load_validation_corpus(corpus_path, roots={"knowhere": root})


def test_load_validation_corpus_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "knowhere"
    fixtures = root / "fixtures"
    fixtures.mkdir(parents=True)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-test")
    link = fixtures / "sample.pdf"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    corpus_path = _write_corpus(tmp_path / "corpus.json", [_document()])

    with pytest.raises(ValueError, match="escape"):
        load_validation_corpus(corpus_path, roots={"knowhere": root})
