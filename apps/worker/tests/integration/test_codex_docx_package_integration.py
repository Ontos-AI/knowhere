from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pymupdf
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

if TYPE_CHECKING:
    from app.services.codex_export.package_builder import ReviewPackageRequest
    from app.services.document_parser.providers.mineru.artifact_contract import (
        MinerUArtifactBundle,
        MinerUArtifactManifest,
    )
    from app.services.document_parser.providers.mineru.local_process import (
        LocalMinerURequest,
    )

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "codex_export"
DOCX_ARTIFACTS = FIXTURE_ROOT / "mineru_docx_artifacts"
sys.path.insert(0, str(FIXTURE_ROOT))

from generate_docx_fixture import generate_docx_fixture  # noqa: E402


class StaticDOCXMinerURunner:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    def run(self, request: LocalMinerURequest) -> MinerUArtifactBundle:
        from app.services.document_parser.providers.mineru.artifact_contract import (
            MinerUArtifactBundle,
            MinerUArtifactManifest,
        )

        pages = json.loads(
            (DOCX_ARTIFACTS / "synthetic_content_list_v2.json").read_text(
                encoding="utf-8"
            )
        )
        source_hash = hashlib.sha256(request.source_path.read_bytes()).hexdigest()
        manifest = MinerUArtifactManifest(
            schema_version="knowhere-mineru-artifacts/1.0",
            status="completed",
            source={
                "filename": request.source_path.name,
                "suffix": ".docx",
                "sha256": source_hash,
                "size_bytes": request.source_path.stat().st_size,
            },
            parser={"name": "MinerU", "backend_effective": "office"},
            execution={
                "mode": "static-local-fixture",
                "offline_requested": True,
                "offline_verified": True,
            },
            document={"logical_page_count": len(pages)},
            artifacts={},
            warnings=(),
            raw={},
        )
        return MinerUArtifactBundle(
            manifest_path=DOCX_ARTIFACTS / "mineru_manifest.json",
            output_root=DOCX_ARTIFACTS,
            markdown_path=DOCX_ARTIFACTS / "synthetic.md",
            middle_json_path=DOCX_ARTIFACTS / "synthetic_middle.json",
            content_list_path=DOCX_ARTIFACTS / "synthetic_content_list.json",
            content_list_v2_path=DOCX_ARTIFACTS / "synthetic_content_list_v2.json",
            images_dir=DOCX_ARTIFACTS / "images",
            manifest=manifest,
        )


def _fake_docx_conversion(*, docx_path: Path, output_dir: Path) -> Path:
    assert docx_path.suffix == ".docx"
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "source.pdf"
    document = pymupdf.open()
    try:
        for page_number in (1, 2):
            page = document.new_page()
            page.insert_text((72, 72), f"Normalized DOCX page {page_number}")
        document.save(destination)
    finally:
        document.close()
    return destination


def _fake_page_renderer(**kwargs: Any) -> list[SimpleNamespace]:
    output_dir = Path(kwargs["output_dir"]) / "fixture-docx-renders"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for page in kwargs["pages"]:
        image_path = output_dir / f"normalized-{page}.png"
        image_path.write_bytes(f"normalized-page-{page}".encode())
        rendered.append(SimpleNamespace(page_index=page, image_path=str(image_path)))
    return rendered


def _request(source: Path, output: Path, project: Path) -> ReviewPackageRequest:
    from app.services.codex_export.package_builder import ReviewPackageRequest

    return ReviewPackageRequest(
        source_path=source,
        output_root=output,
        mineru_project_path=project,
        backend="pipeline",
        method="auto",
        language="en",
        requested_pages=(2,),
        include_table_pages=True,
        include_image_pages=True,
        dpi=144,
        offline=True,
        force=False,
        keep_work_dir=False,
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_docx_fixture_uses_office_blocks_and_normalized_pdf_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.codex_export import package_builder, page_selection

    source = generate_docx_fixture(tmp_path / "synthetic-review.docx")
    project = tmp_path / "mineru-project"
    project.mkdir()
    monkeypatch.setattr(package_builder, "LocalMinerURunner", StaticDOCXMinerURunner)
    monkeypatch.setattr(
        package_builder, "render_docx_to_normalized_pdf", _fake_docx_conversion
    )
    monkeypatch.setattr(
        package_builder, "probe_libreoffice_version", lambda: "fixture-24.2"
    )
    monkeypatch.setattr(page_selection, "render_document_pages", _fake_page_renderer)

    result = package_builder.build_codex_review_package(
        _request(source, tmp_path / "docx-package", project)
    )

    package = result.package_root
    blocks = _jsonl(package / "structured" / "blocks.jsonl")
    findings = _jsonl(package / "structured" / "extraction_findings.jsonl")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert (package / "native" / "source.docx").read_bytes() == source.read_bytes()
    assert (package / "normalized" / "source.pdf").is_file()
    assert (package / "pages" / "page-0002.png").is_file()
    assert not (package / "pages" / "page-0001.png").exists()
    assert manifest["mineru"]["parser"]["backend_effective"] == "office"
    assert manifest["conversion"]["page_number_semantics"] == "normalized_pdf"
    assert manifest["conversion"]["selected_pages"] == [2]

    assert all(block["source_locator"]["kind"] == "office_logical_page" for block in blocks)
    assert all(
        block["source_locator"]["normalized_pdf_page_number"] is None
        and block["source_locator"]["normalized_pdf_mapping_status"] == "unmapped"
        for block in blocks
    )
    heading = next(block for block in blocks if block["block_type"] == "title")
    assert heading["source_locator"]["anchor"] == "heading-1-overview"
    assert any(finding["category"] == "docx_rendering" for finding in findings)

    metadata_paths = sorted((package / "tables").glob("*.metadata.json"))
    assert len(metadata_paths) == 2
    metadata = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_paths]
    assert {item["csv_fidelity"] for item in metadata} == {
        "best_effort_simple",
        "lossy_complex",
    }
