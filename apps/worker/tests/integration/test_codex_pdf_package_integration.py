from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

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
PDF_ARTIFACTS = FIXTURE_ROOT / "mineru_pdf_artifacts"
sys.path.insert(0, str(FIXTURE_ROOT))

from generate_pdf_fixture import generate_pdf_fixture  # noqa: E402


class StaticPDFMinerURunner:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    def run(self, request: LocalMinerURequest) -> MinerUArtifactBundle:
        from app.services.document_parser.providers.mineru.artifact_contract import (
            MinerUArtifactBundle,
            MinerUArtifactManifest,
        )

        pages = json.loads(
            (PDF_ARTIFACTS / "synthetic_content_list_v2.json").read_text(
                encoding="utf-8"
            )
        )
        source_hash = hashlib.sha256(request.source_path.read_bytes()).hexdigest()
        manifest = MinerUArtifactManifest(
            schema_version="knowhere-mineru-artifacts/1.0",
            status="completed",
            source={
                "filename": request.source_path.name,
                "suffix": ".pdf",
                "sha256": source_hash,
                "size_bytes": request.source_path.stat().st_size,
            },
            parser={
                "name": "MinerU",
                "backend_effective": "pipeline",
                "method": "auto",
            },
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
            manifest_path=PDF_ARTIFACTS / "mineru_manifest.json",
            output_root=PDF_ARTIFACTS,
            markdown_path=PDF_ARTIFACTS / "synthetic.md",
            middle_json_path=PDF_ARTIFACTS / "synthetic_middle.json",
            content_list_path=PDF_ARTIFACTS / "synthetic_content_list.json",
            content_list_v2_path=PDF_ARTIFACTS / "synthetic_content_list_v2.json",
            images_dir=PDF_ARTIFACTS / "images",
            manifest=manifest,
        )


def _request(source: Path, output: Path, project: Path) -> ReviewPackageRequest:
    from app.services.codex_export.package_builder import ReviewPackageRequest

    return ReviewPackageRequest(
        source_path=source,
        output_root=output,
        mineru_project_path=project,
        backend="pipeline",
        method="auto",
        language="en",
        requested_pages=(1,),
        include_table_pages=True,
        include_image_pages=True,
        dpi=144,
        offline=True,
        force=False,
        keep_work_dir=False,
    )


def _fake_page_renderer(**kwargs: Any) -> list[SimpleNamespace]:
    output_dir = Path(kwargs["output_dir"]) / "fixture-page-renders"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for page in kwargs["pages"]:
        image_path = output_dir / f"source-{page}.png"
        image_path.write_bytes(f"synthetic-png-page-{page}".encode())
        rendered.append(SimpleNamespace(page_index=page, image_path=str(image_path)))
    return rendered


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_static_pdf_fixture_build_is_complete_and_reproducible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.codex_export import package_builder, page_selection

    source = generate_pdf_fixture(tmp_path / "synthetic-review.pdf")
    project = tmp_path / "mineru-project"
    project.mkdir()
    monkeypatch.setattr(package_builder, "LocalMinerURunner", StaticPDFMinerURunner)
    monkeypatch.setattr(page_selection, "render_document_pages", _fake_page_renderer)

    first = package_builder.build_codex_review_package(
        _request(source, tmp_path / "first", project)
    )
    second = package_builder.build_codex_review_package(
        _request(source, tmp_path / "second", project)
    )

    first_blocks = _jsonl(first.package_root / "structured" / "blocks.jsonl")
    second_blocks = _jsonl(second.package_root / "structured" / "blocks.jsonl")
    first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    first_tree = json.loads(
        (first.package_root / "structured" / "document_tree.json").read_text(
            encoding="utf-8"
        )
    )
    second_tree = json.loads(
        (second.package_root / "structured" / "document_tree.json").read_text(
            encoding="utf-8"
        )
    )
    assert first.document_id == second.document_id
    assert [block["block_id"] for block in first_blocks] == [
        block["block_id"] for block in second_blocks
    ]
    assert [block["content_sha256"] for block in first_blocks] == [
        block["content_sha256"] for block in second_blocks
    ]
    assert first_blocks == second_blocks
    assert [node["node_id"] for node in first_tree["nodes"]] == [
        node["node_id"] for node in second_tree["nodes"]
    ]
    assert [
        json.loads(path.read_text(encoding="utf-8"))["table_id"]
        for path in sorted((first.package_root / "tables").glob("*.metadata.json"))
    ] == [
        json.loads(path.read_text(encoding="utf-8"))["table_id"]
        for path in sorted((second.package_root / "tables").glob("*.metadata.json"))
    ]
    assert {
        path.name: path.read_bytes()
        for path in (first.package_root / "tables").iterdir()
        if path.suffix in {".html", ".csv"}
    } == {
        path.name: path.read_bytes()
        for path in (second.package_root / "tables").iterdir()
        if path.suffix in {".html", ".csv"}
    }
    assert sorted(path.name for path in (first.package_root / "pages").glob("*.png")) == sorted(
        path.name for path in (second.package_root / "pages").glob("*.png")
    )
    assert {block["source_locator"]["page_number"] for block in first_blocks} == {
        1,
        2,
    }

    table_blocks = [block for block in first_blocks if block["block_type"] == "table"]
    assert len(table_blocks) == 2
    assert all(
        any(asset.get("relative_path", "").startswith("tables/") for asset in block["assets"])
        for block in table_blocks
    )
    assert (first.package_root / "pages" / "page-0001.png").is_file()
    assert (first.package_root / "pages" / "page-0002.png").is_file()

    assert first_manifest["counts"] == {
        "blocks": len(first_blocks),
        "findings": len(
            _jsonl(first.package_root / "structured" / "extraction_findings.jsonl")
        ),
        "pages": len(list((first.package_root / "pages").glob("*.png"))),
        "tables": len(list((first.package_root / "tables").glob("*.metadata.json"))),
    }
    root = next(node for node in first_tree["nodes"] if node["node_id"] == "sec_root")
    details = next(
        node for node in first_tree["nodes"] if node["title"] == "1.1 Results"
    )
    assert (root["start_page_number"], root["end_page_number"]) == (1, 2)
    assert (details["start_page_number"], details["end_page_number"]) == (2, 2)
    assert first_manifest["conversion"]["selected_pages"] == [1, 2]
    assert second_manifest["conversion"]["selected_pages"] == [1, 2]


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_MINERU_E2E") != "1",
    reason="set RUN_LOCAL_MINERU_E2E=1 for the local model-backed smoke test",
)
def test_real_local_mineru_pdf_export_is_offline(tmp_path: Path) -> None:
    project_value = os.environ.get("MINERU_LOCAL_PROJECT_PATH")
    if not project_value:
        pytest.fail("MINERU_LOCAL_PROJECT_PATH is required for the opt-in E2E test")
    source = generate_pdf_fixture(tmp_path / "real-mineru.pdf")

    result = build_codex_review_package(
        _request(source, tmp_path / "real-package", Path(project_value))
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["offline"]["requested"] is True
    assert manifest["mineru"]["execution"]["offline_requested"] is True
    assert "server_url" not in manifest["mineru"]["execution"]
    assert result.block_count > 0
