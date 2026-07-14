from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.codex_export.package_builder import (  # noqa: E402
    ReviewPackageError,
    ReviewPackageRequest,
    build_codex_review_package,
)
from app.services.codex_export.page_selection import RenderedPage  # noqa: E402
from app.services.document_parser.providers.mineru.artifact_contract import (  # noqa: E402
    MinerUArtifactBundle,
    MinerUArtifactManifest,
)
from shared.core.exceptions.domain_exceptions import (  # noqa: E402
    LibreOfficeServiceException,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf(path: Path, page_count: int = 2) -> None:
    document = pymupdf.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"Synthetic page {page_number}")
    document.save(path)
    document.close()


def _source(tmp_path: Path, suffix: str) -> Path:
    source = tmp_path / f"source{suffix}"
    if suffix == ".pdf":
        _pdf(source)
    else:
        source.write_bytes(b"PK synthetic docx")
    return source


def _bundle(tmp_path: Path, source: Path) -> MinerUArtifactBundle:
    root = tmp_path / f"fake-mineru-{source.suffix[1:]}"
    parse_dir = root / source.stem / ("office" if source.suffix == ".docx" else "auto")
    images_dir = parse_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "table-1.png").write_bytes(b"synthetic-table-image")
    markdown = parse_dir / f"{source.stem}.md"
    middle = parse_dir / f"{source.stem}_middle.json"
    content_list = parse_dir / f"{source.stem}_content_list.json"
    content_list_v2 = parse_dir / f"{source.stem}_content_list_v2.json"
    markdown.write_text("# 1. Scope\n\nSynthetic body", encoding="utf-8")
    middle.write_text(json.dumps({"pdf_info": [{"page_idx": 0}]}), encoding="utf-8")
    content_list.write_text(json.dumps([]), encoding="utf-8")
    content_list_v2.write_text(
        json.dumps(
            [
                [
                    {
                        "type": "title",
                        "content": {
                            "title_content": [
                                {"type": "text", "content": "1. Scope"}
                            ],
                            "level": 1,
                        },
                    },
                    {
                        "type": "paragraph",
                        "content": {
                            "paragraph_content": [
                                {
                                    "type": "text",
                                    "content": "Synthetic ≤ 10 µA body",
                                }
                            ]
                        },
                    },
                ],
                [
                    {
                        "type": "table",
                        "content": {
                            "image_source": {"path": "images/table-1.png"},
                            "table_caption": [
                                {"type": "text", "content": "Table 1"}
                            ],
                            "table_footnote": [],
                            "html": (
                                "<table><tr><td>Metric</td><td>Value</td></tr>"
                                "<tr><td>Leakage</td><td>≤ 10 µA</td></tr></table>"
                            ),
                            "table_type": "simple_table",
                            "table_nest_level": 1,
                        },
                    }
                ],
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    raw_manifest = {
        "schema_version": "knowhere-mineru-artifacts/1.0",
        "status": "completed",
        "source": {
            "filename": source.name,
            "suffix": source.suffix,
            "sha256": _sha256(source),
            "size_bytes": source.stat().st_size,
        },
        "parser": {
            "name": "MinerU",
            "version": "3.4.4",
            "git_commit": "79d6d8d",
            "backend_requested": "pipeline",
            "backend_effective": "office" if source.suffix == ".docx" else "pipeline",
            "method": "auto",
            "language": "en",
        },
        "execution": {
            "mode": "local-direct-python",
            "offline_requested": True,
            "offline_verified": False,
        },
        "document": {"logical_page_count": 2},
        "artifacts": {},
        "warnings": [],
    }
    manifest_path = root / "mineru_manifest.json"
    manifest_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
    manifest = MinerUArtifactManifest(
        schema_version="knowhere-mineru-artifacts/1.0",
        status="completed",
        source=raw_manifest["source"],
        parser=raw_manifest["parser"],
        execution=raw_manifest["execution"],
        document=raw_manifest["document"],
        artifacts={},
        warnings=(),
        raw=raw_manifest,
    )
    return MinerUArtifactBundle(
        manifest_path=manifest_path,
        output_root=root,
        markdown_path=markdown,
        middle_json_path=middle,
        content_list_path=content_list,
        content_list_v2_path=content_list_v2,
        images_dir=images_dir,
        manifest=manifest,
    )


def _request(
    tmp_path: Path,
    source: Path,
    *,
    requested_pages: tuple[int, ...] = (),
    force: bool = False,
    keep_work_dir: bool = False,
) -> ReviewPackageRequest:
    project = tmp_path / "MinerU-project"
    project.mkdir(exist_ok=True)
    return ReviewPackageRequest(
        source_path=source,
        output_root=tmp_path / f"package-{source.suffix[1:]}",
        mineru_project_path=project,
        backend="pipeline",
        method="auto",
        language="en",
        requested_pages=requested_pages,
        include_table_pages=True,
        include_image_pages=False,
        dpi=200,
        offline=True,
        force=force,
        keep_work_dir=keep_work_dir,
    )


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    bundle: MinerUArtifactBundle,
) -> None:
    from app.services.codex_export import package_builder

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _request):
            return bundle

    monkeypatch.setattr(package_builder, "LocalMinerURunner", FakeRunner)


def _install_fake_page_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.codex_export import package_builder

    def fake_render_review_pages(*, pdf_path, pages, output_dir, dpi, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered = []
        for page in pages:
            path = output_dir / f"page-{page:04d}.png"
            path.write_bytes(f"png-{page}".encode())
            rendered.append(
                RenderedPage(
                    page_number=page,
                    output_path=path,
                    dpi=dpi,
                    width_points=612,
                    height_points=792,
                )
            )
        return rendered

    monkeypatch.setattr(
        package_builder, "render_review_pages", fake_render_review_pages
    )


def test_successful_pdf_package_contains_portable_review_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    _install_fake_page_renderer(monkeypatch)

    result = build_codex_review_package(
        _request(tmp_path, source, requested_pages=(1,))
    )

    package = result.package_root
    assert (package / "native" / "source.pdf").is_file()
    assert (package / "derivatives" / "document.md").is_file()
    assert (package / "structured" / "blocks.jsonl").is_file()
    assert (package / "structured" / "document_tree.json").is_file()
    assert (package / "structured" / "extraction_findings.jsonl").is_file()
    assert list((package / "tables").glob("T-blk_*.html"))
    assert (package / "pages" / "page-0001.png").is_file()
    assert (package / "pages" / "page-0002.png").is_file()
    assert (package / "CODEX_REVIEW_INSTRUCTIONS.md").is_file()
    assert result.block_count == 3
    assert result.table_count == 1
    assert result.page_count == 2


def test_docx_without_libreoffice_completes_structured_package_when_no_pages_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".docx")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    from app.services.codex_export import package_builder

    def unavailable(**_kwargs):
        raise LibreOfficeServiceException(
            internal_message="LibreOffice unavailable",
            operation="resolve_binary",
        )

    monkeypatch.setattr(package_builder, "render_docx_to_normalized_pdf", unavailable)

    result = build_codex_review_package(_request(tmp_path, source))

    assert (result.package_root / "structured" / "blocks.jsonl").is_file()
    assert not (result.package_root / "normalized" / "source.pdf").exists()
    findings = (
        result.package_root / "structured" / "extraction_findings.jsonl"
    ).read_text(encoding="utf-8")
    assert "docx_rendering" in findings
    assert "normalized_pdf_mapping_status" in (
        result.package_root / "structured" / "blocks.jsonl"
    ).read_text(encoding="utf-8")


def test_existing_output_is_rejected_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    request = _request(tmp_path, source)
    request.output_root.mkdir()
    (request.output_root / "keep.txt").write_text("keep", encoding="utf-8")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))

    with pytest.raises(ReviewPackageError, match="already exists"):
        build_codex_review_package(request)

    assert (request.output_root / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_build_failure_is_atomic_and_leaves_no_complete_looking_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    request = _request(tmp_path, source)
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    from app.services.codex_export import package_builder

    monkeypatch.setattr(
        package_builder,
        "normalize_content_list_v2",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        build_codex_review_package(request)

    assert not request.output_root.exists()
    assert not list(tmp_path.glob(f".{request.output_root.name}.building-*"))


def test_final_manifest_hashes_match_every_inventoried_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    _install_fake_page_renderer(monkeypatch)

    result = build_codex_review_package(_request(tmp_path, source))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "codex-review-package/1.0"
    for artifact in manifest["artifacts"]:
        path = result.package_root / artifact["path"]
        assert path.is_file()
        assert _sha256(path) == artifact["sha256"]
        assert path.stat().st_size == artifact["size_bytes"]


def test_instructions_define_evidence_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    _install_fake_page_renderer(monkeypatch)

    result = build_codex_review_package(_request(tmp_path, source))
    instructions = (result.package_root / "CODEX_REVIEW_INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )

    assert "navigation only" in instructions
    assert "native page verification" in instructions
    assert "document ID" in instructions
    assert "block ID" in instructions
    assert "table ID" in instructions
    assert "generated summaries" in instructions
    assert "Pass/Fail" in instructions


def test_manifest_is_portable_and_package_does_not_leak_environment_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    secret = "do-not-leak-this-api-key"
    monkeypatch.setenv("MINERU_API_KEYS", secret)
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    _install_fake_page_renderer(monkeypatch)

    result = build_codex_review_package(_request(tmp_path, source))
    manifest_text = result.manifest_path.read_text(encoding="utf-8")

    assert str(tmp_path) not in manifest_text
    for path in result.package_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".md", ".log", ".csv", ".html"}:
            assert secret not in path.read_text(encoding="utf-8-sig")


def test_package_gitignore_and_native_source_hash_match_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, ".pdf")
    _install_fake_runner(monkeypatch, _bundle(tmp_path, source))
    _install_fake_page_renderer(monkeypatch)

    result = build_codex_review_package(_request(tmp_path, source))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert (result.package_root / ".gitignore").read_text(encoding="utf-8") == (
        "*\n!.gitignore\n"
    )
    native_path = result.package_root / manifest["source"]["native_path"]
    assert _sha256(native_path) == manifest["source"]["sha256"] == _sha256(source)
