from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from app.services.codex_export.validation_corpus import load_validation_corpus
from app.services.codex_export.package_builder import ReviewPackageResult
from app.services.codex_export.validation_report import write_validation_reports
from app.services.codex_export.validation_runner import (
    ValidationOptions,
    run_validation_corpus,
)
from scripts import validate_codex_export_corpus as batch_cli


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
        ([_document(id="a" * 65)], "document id"),
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


def _fake_package(request: object) -> ReviewPackageResult:
    output_root = request.output_root
    package_root = output_root / "package"
    structured = package_root / "structured"
    tables = package_root / "tables"
    metadata = package_root / "metadata"
    structured.mkdir(parents=True)
    tables.mkdir()
    metadata.mkdir()
    blocks = structured / "blocks.jsonl"
    blocks.write_text('{"block_id":"b1","content_sha256":"abc"}\n', encoding="utf-8")
    tree = structured / "document_tree.json"
    tree.write_text('{"nodes":[{"node_id":"root"}]}', encoding="utf-8")
    findings = structured / "extraction_findings.jsonl"
    findings.write_text(
        '{"category":"table_conversion","message":"content must stay private"}\n',
        encoding="utf-8",
    )
    table = tables / "T-b1.metadata.json"
    table.write_text(
        json.dumps({"table_id": "t1", "csv_fidelity": "lossy_complex"}),
        encoding="utf-8",
    )
    artifacts = []
    for path in (blocks, tree, findings, table):
        data = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(package_root).as_posix(),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "codex-review-package/1.0",
        "status": "completed",
        "counts": {"blocks": 1, "tables": 1, "pages": 0, "findings": 1},
        "artifacts": artifacts,
    }
    manifest_path = metadata / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return ReviewPackageResult(
        package_root=package_root,
        manifest_path=manifest_path,
        document_id=request.source_path.stem,
        block_count=1,
        table_count=1,
        page_count=0,
    )


def test_batch_runner_continues_in_order_and_writes_private_reports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-root"
    fixtures = root / "fixtures"
    fixtures.mkdir(parents=True)
    for name in ("first.pdf", "second.pdf"):
        (fixtures / name).write_bytes(b"%PDF-private-source-content")
    corpus_path = _write_corpus(
        tmp_path / "corpus.json",
        [
            _document(id="first", path="fixtures/first.pdf"),
            _document(id="second", path="fixtures/second.pdf", expected_status="failed"),
        ],
    )
    corpus = load_validation_corpus(corpus_path, roots={"knowhere": root})
    calls: list[str] = []

    def builder(request: object) -> ReviewPackageResult:
        calls.append(request.source_path.name)
        if request.source_path.name == "second.pdf":
            raise RuntimeError(
                f"failed at {request.source_path} api_key=super-secret-value"
            )
        return _fake_package(request)

    report = run_validation_corpus(
        corpus,
        ValidationOptions(
            output_root=tmp_path / "output",
            mineru_project_path=tmp_path / "mineru",
            repeat=2,
        ),
        package_builder=builder,
    )

    assert calls == ["first.pdf", "first.pdf", "second.pdf", "second.pdf"]
    assert [result.document_id for result in report.results] == [
        "first",
        "first",
        "second",
        "second",
    ]
    assert all(result.expectation_matched for result in report.results)
    assert all(result.reproducible for result in report.results)
    first = report.results[0]
    assert first.table_fidelity == {"lossy_complex": 1}
    assert first.finding_categories == {"table_conversion": 1}
    assert first.artifacts_verified is True
    assert first.source_sha256 == hashlib.sha256(
        b"%PDF-private-source-content"
    ).hexdigest()

    json_path, html_path = write_validation_reports(report, tmp_path / "reports")
    serialized = json_path.read_text(encoding="utf-8") + html_path.read_text(
        encoding="utf-8"
    )
    assert "private-source-content" not in serialized
    assert str(root) not in serialized
    assert "super-secret-value" not in serialized
    assert "content must stay private" not in serialized
    assert report.summary["expectation_mismatches"] == 0


def test_batch_runner_rejects_tampered_artifact(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "fixtures" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-test")
    corpus = load_validation_corpus(
        _write_corpus(tmp_path / "corpus.json", [_document()]),
        roots={"knowhere": root},
    )

    def tampered_builder(request: object) -> ReviewPackageResult:
        result = _fake_package(request)
        (result.package_root / "structured" / "blocks.jsonl").write_text(
            "tampered", encoding="utf-8"
        )
        return result

    report = run_validation_corpus(
        corpus,
        ValidationOptions(
            output_root=tmp_path / "output",
            mineru_project_path=tmp_path / "mineru",
        ),
        package_builder=tampered_builder,
    )

    assert report.results[0].actual_status == "failed"
    assert report.results[0].artifacts_verified is False
    assert report.results[0].error_type == "PackageAuditError"


def test_batch_cli_defaults_to_offline_sequential_validation(tmp_path: Path) -> None:
    args = batch_cli.build_parser().parse_args(
        [
            "--corpus",
            str(tmp_path / "corpus.json"),
            "--output",
            str(tmp_path / "output"),
            "--mineru-project",
            str(tmp_path / "MinerU"),
        ]
    )

    assert args.repeat == 1
    assert args.backend == "pipeline"
    assert args.method == "auto"
    assert args.dpi == 144
    assert args.offline is True
